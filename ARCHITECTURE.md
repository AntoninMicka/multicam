# Architektura

## Komponenty

1. **PWA kamera (Vue/TypeScript)** – kamera, synchronizační časy, lokální blokové uložení, stav klienta a upload.
2. **Režisérské UI** – správa relace, připravenost zařízení, start/stop a přehled přenosů.
3. **FastAPI server** – distribuce PWA, WebSocket řízení, příjem bloků, kontrola integrity a manifest relace.
4. **Diskové úložiště notebooku** – neměnné surové soubory organizované podle relace a zařízení.
5. **Synchronizační krok** – určení časového posunu videí podle klapky a uložení korekcí do manifestu.
6. **Top-down analýza** – reprodukovatelný postprocessing ověřeného videa,
   kalibrace roviny scény a export trajektorií; běží odděleně od capture serveru.
7. **Discovery backendů** – periodický UDP heartbeat na `239.255.77.77:47777`;
   funguje v LAN i nad virtuálním rozhraním ZeroTier. Jde pouze o nalezení pultů,
   relace a úložiště zůstávají vlastnictvím konkrétního backendu.
8. **Federace pultů** – leader je autoritou pro relace, aktivní relaci, globálně
   unikátní hlavní/top-down roli a řídicí povely. Follower replikuje lokálně
   ověřené záznamy leaderu přes trvalou retry frontu. Opačný směr je volitelná
   záloha; živé náhledy zůstávají lokální.

## Datový tok

`režisér -> WebSocket povel -> telefony -> lokální záznam do IndexedDB -> chunkovaný HTTPS upload -> verifikace serverem -> potvrzení -> řízená čistka telefonu`

## Minimální struktura relace

```text
sessions/<session_id>/
  session.json
  devices/<device_id>/
    device.json
    video.webm
    timing.jsonl
    checksums.json
```

Časová metadata jako JSON Lines usnadní průběžný zápis a obnovu. Každý řádek má obsahovat monotónní čas od začátku záznamu, případný UTC čas a typ synchronizační události.

## Důležitá omezení

- PWA API pro kameru, polohu a orientaci jsou závislá na HTTPS, oprávněních, OS a konkrétním prohlížeči.
- PWA není zárukou spolehlivého běhu na pozadí; obrazovka a aplikace musí během záznamu zůstat aktivní.
- Příkaz po WebSocketu nesynchronizuje fyzický začátek snímání přesně. Síťové časy pomohou s hrubým zarovnáním, obrazová/zvuková klapka s přesným postprocessingem.
- Ovládání svítilny není napříč mobilními prohlížeči jednotně dostupné. Externí viditelné světlo je bezpečnější synchronizační fallback.
- Automatické mazání bez ověřeného kontrolního součtu a zálohy je nepřijatelné riziko ztráty dat.

## Rozsah MVP

Capture MVP končí ověřeným uložením a časovým zarovnáním. Tento repozitář nově
obsahuje také navazující, oddělenou OpenCV analýzu top-down kamery. Její
verzovaný výstup je odvozený artefakt a nikdy nemění zdrojové záznamy.
