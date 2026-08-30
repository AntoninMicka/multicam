# MultiCam PWA Capture

Lokální systém pro provizorní synchronizovaný záznam videa z několika telefonů. Telefony nahrávají offline do lokálního úložiště, notebook v dočasné Wi-Fi síti zajišťuje řízení relace, příjem dat, kontrolu integrity a společné uložení.

## Cíl první verze

Spolehlivě pořídit z několika telefonů jednu společnou relaci obsahující videa a pouze metadata nutná k jejich synchronizaci, přenést ji na notebook a data na telefonech odstranit až po ověření kontrolního součtu.

## Navržený stack

- mobilní PWA: Vue 3 + Vite + TypeScript + `vite-plugin-pwa`
- lokální server: Python + FastAPI + WebSocket
- serverový nástroj: FFmpeg pro bezztrátové doplnění WebM indexu pro přehrávání
- lokální úložiště telefonu: IndexedDB
- desktopový režisérský pult: nejprve webové UI, později volitelně Qt

## Rozsah projektu

Projekt řeší výhradně:

- lokální pořízení videa na více telefonech;
- synchronizaci vzniklých záznamů;
- bezpečný přenos a společné uložení na centrálním notebooku.

Mimo scope jsou kalibrace prostoru, rekonstrukce scény, detekce a sledování osob, pose estimation, AI analýza, vícekamerová fúze, ComfyUI/Ollama a optimalizace zpracování v C++.

## Dokumentace

- [ROADMAP.md](ROADMAP.md) – fáze vývoje, akceptační kritéria a terénní checklist
- [ARCHITECTURE.md](ARCHITECTURE.md) – komponenty, datový tok a zásadní technická omezení

## Lokální spuštění

Nejjednodušší spuštění celé aplikace:

```bash
./run.sh
```

Skript podle potřeby vytvoří `.venv`, nainstaluje Python a npm závislosti, sestaví PWA, vygeneruje lokální certifikát a spustí FastAPI server. Výchozí adresa je `https://0.0.0.0:8000`; nastavení lze změnit například takto:

```bash
MULTICAM_HOST=127.0.0.1 MULTICAM_PORT=9000 ./run.sh
```

Další argumenty se předají přímo Uvicornu, například `./run.sh --reload`.

### Lokální HTTPS certifikát

Při prvním spuštění vznikne lokální certifikační autorita a serverový certifikát v ignorovaném adresáři `certs/`. Soubor `certs/local-ca.cert.crt` je potřeba nainstalovat jako důvěryhodnou CA do každého telefonu. PEM varianta stejného veřejného certifikátu zůstává v `certs/local-ca.cert.pem`. Privátní soubor `certs/local-ca.key.pem` se nesmí kopírovat ani zveřejňovat.

Serverový certifikát automaticky zahrnuje `localhost`, `127.0.0.1` a LAN adresy zjištěné při jeho vytvoření. Pokud je automatické zjištění nedostupné nebo se IP notebooku změnila, certifikát obnovte explicitně:

```bash
MULTICAM_CERT_IPS=192.168.1.10 ./scripts/generate-local-cert.sh --force
```

Více adres lze oddělit čárkou. Pro jednorázové vývojové spuštění bez TLS lze použít `MULTICAM_HTTPS=0 ./run.sh`.

### Izolovaný Wi‑Fi hotspot

Na Linuxu s NetworkManagerem lze server i ostrovní AP spustit jedním příkazem:

```bash
./run-hotspot.sh
```

Wrapper si vyžádá oprávnění správce, vytvoří WPA2 síť `MultiCam`, nastaví notebook na `10.42.0.1`, spustí DHCP a wildcard DNS a firewallem zablokuje forwarding klientů do jiných sítí. HTTP požadavky a běžné kontroly captive portálu skončí na lokální úvodní stránce s odkazem na HTTPS aplikaci a instalačním CA certifikátem. Po ukončení serveru wrapper odstraní pouze vlastní síťový profil, DNS proces, captive portal a firewallovou tabulku.

Název, heslo a rozhraní lze nastavit například takto:

```bash
MULTICAM_WIFI_IFACE=wlan0 MULTICAM_HOTSPOT_SSID=Nataceni MULTICAM_HOTSPOT_PASSWORD=bezpecne-heslo ./run-hotspot.sh
```

Režisérský pult po spuštění zobrazí QR pro připojení k Wi‑Fi a druhý QR s adresou aplikace. Výchozí síť je záměrně bez přístupu k internetu.

### Ruční spuštění

Backend:

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
.venv/bin/uvicorn backend.app.main:app --reload
```

Frontend v druhém terminálu:

```bash
cd frontend
npm install
npm run dev
```

Režisérský pult i kamerový klient jsou součástí stejné PWA na `http://localhost:5173`. Produkční build vytvoří `frontend/dist`; FastAPI jej při dalším startu automaticky zpřístupní na portu 8000.

V sandboxovém režimu existuje vždy jedna aktuální relace, kterou klienti vyhledají automaticky. Není proto nutné opisovat její ID. Zařízení si při připojení zvolí roli hlavní, top-over nebo vedlejší kamery; vedlejších kamer může být libovolný počet. Režisér může hlavní kameře přes WebSocket poslat povel k celoobrazovkové světelné klapce; stejnou klapku lze na hlavní kameře otestovat lokálně.

Pro sestavení scény může režisér zapnout živé náhledy všech připojených kamer. Kamery posílají přes řídicí WebSocket jeden JPEG snímek za sekundu, maximálně 320 px široký a s úspornou kvalitou. Náhledy se během ostrého záznamu automaticky pozastaví a lze je z pultu zcela vypnout.

Spuštění a zastavení záznamu lze ovládat z režisérského pultu i z hlavní kamery. Povel se přenese na všechny připojené kamery. Po zastavení klient vypočítá SHA-256 a automaticky odešle záznam ve 4MB blocích. Server podporuje opakované bloky a navázání podle již přijatých částí, výsledný soubor skládá atomicky a označí zařízení jako `verified` až po kontrole velikosti a SHA-256. Data se ukládají do `data/sessions/`; umístění lze změnit proměnnou `MULTICAM_DATA_DIR`.

Ke každému videu klient vytváří párovaný telemetrický soubor JSON Lines s monotónními a UTC časy startu, stopu, pravidelných vzorků hodin a přijetí synchronizační klapky. Každý vzorek obsahuje také poslední GNSS pozici, orientaci zařízení a skutečné nastavení zoomu kamerového tracku. Webová API neposkytují spolehlivě fyzický úhel záběru, proto je `field_of_view_deg` bez kalibrace telefonu explicitně `null`; `zoom_ratio`, rozlišení a ostatní dostupné hodnoty se ukládají přímo. Zařízení přejde do stavu `verified` až po ověření videa i telemetrie.

Dvě sekundy po povelu ke spuštění záznamu server automaticky spustí identifikační světelnou klapku: úvodní synchronizační záblesk hlavní kamery, samostatný záblesk každé top/vedlejší kamery a závěrečný dvojitý podpis hlavní kamery. Každý krok se uloží do telemetrie všech kamer a do společného `events.jsonl` relace. Cílová kamera zkusí přes `MediaStreamTrack.applyConstraints()` zapnout hardwarovou svítilnu (`torch`), což je určeno především pro Chrome na Androidu. Pokud prohlížeč nebo telefon tuto možnost neposkytne, použije se celoobrazovkový bílý záblesk.

Každý blok z `MediaRecorder` a každá telemetrická událost se průběžně ukládají do IndexedDB. Po reloadu aplikace zobrazí dokončené i přerušené lokální záznamy a dovolí jejich upload zopakovat. Kamerový pohled prohledává celý lokální archiv prohlížeče, nikoli pouze záznamy současného `device_id`, takže najde také data ze starší relace, role nebo registrace zařízení. Ověřenou lokální kopii lze smazat pouze samostatným tlačítkem a po výslovném potvrzení uživatele.

REST API dokumentace je za běhu dostupná na `https://localhost:8000/docs`. Manifest relace a časové události mají verzovaná schémata v adresáři [`schemas`](schemas).

Režisérský pult nabízí seznam uložených relací. Po výběru zobrazí matici všech ověřených kamerových záznamů. Každý přehrávač načte párovanou telemetrii a podle aktuálního času videa zobrazuje nejbližší číselný vzorek: zoom, GNSS souřadnice, přesnost a orientační úhly. Manifesty relací se ukládají jako `data/sessions/<session_id>/session.json`, takže seznam přežije restart serveru.

Samostatný archiv režisérského pultu zobrazuje všechny záznamy napříč relacemi seskupené podle klapky. Jednotlivý záběr i celou skupinu lze po výslovném potvrzení odstranit ze serveru; odstraní se video, párovaná telemetrie i příslušná uploadová metadata.

## Aktuální stav

Projekt je uzavřený jako funkční MVP pro zamýšlené lokální použití.
Relace a zařízení se ukládají do manifestů na disku a po restartu serveru se znovu načtou.
Záznam, telemetrie, obnovitelný chunkovaný upload, kontrola integrity, historie relací,
lokální HTTPS a izolovaný hotspot jsou implementované. Nedokončené oblasti a pořadí
dalších prací jsou vedené v [roadmapě](ROADMAP.md), zejména potvrzování řídicích povelů,
měření časového offsetu, odolnost mobilního záznamu a terénní integrační testy.

Před připojením kamery lze zvolit profil **Kompatibilní** (640×480/24),
**Vyvážený** (1280×720/30, doporučený) nebo **Vysoce kvalitní (4K)**
(3840×2160/30 s automatickým fallbackem na nižší podporované rozlišení).
Prohlížeč z nabídky profilu vybere podporovaný WebM/MP4 kodek a aplikace následně
zobrazí a uloží skutečně dosažené rozlišení, FPS a MIME typ.

## Definice úspěchu MVP

- všechna zařízení se připojí k jedné relaci a zobrazí stav připravenosti;
- režisér spustí a zastaví záznam;
- každé zařízení lokálně uloží video a telemetrii se společným ID relace;
- přerušený upload lze obnovit nebo bezpečně zopakovat;
- server ověří velikost a SHA-256 a teprve potom povolí smazání lokální kopie;
- výsledná videa lze časově zarovnat podle obrazové klapky a časových metadat.
