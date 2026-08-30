# Roadmapa

## 0. Rozhodnutí a ověření zařízení

- [ ] Sepsat cílové telefony, OS, prohlížeče, rozlišení, kodeky a volné místo.
- [ ] Ověřit `getUserMedia`, `MediaRecorder`, IndexedDB, GPS a `DeviceOrientation` na každém telefonu.
- [ ] Ověřit oprávnění senzorů, zejména explicitní souhlas na iOS.
- [ ] Zvolit podporovaný profil videa podle nejslabšího telefonu; nezačínat automaticky ve 4K.
- [ ] Připravit lokální HTTPS (důvěryhodný certifikát v telefonech), protože kamera, poloha a senzory vyžadují secure context.
- [ ] Změřit dostupné místo a odhadnout velikost jedné relace s bezpečnostní rezervou.

**Hotovo, když:** všech pět cílových telefonů projde krátkým kompatibilitním testem a známe společný formát záznamu.

## 1. Kostra systému a relace

- [ ] Založit Vue 3/Vite/TypeScript PWA s manifestem a offline app shellem.
- [ ] Založit FastAPI server pro statické soubory, REST API a WebSocket.
- [ ] Zavést role `director` a `camera` a registraci zařízení.
- [ ] Zavést `session_id`, `device_id`, stavový automat (`disconnected`, `ready`, `recording`, `stored`, `uploading`, `verified`).
- [ ] V režisérském UI zobrazit připojená zařízení, baterii, místo, oprávnění a připravenost.
- [ ] Definovat verzované JSON schéma manifestu relace a telemetrie.

**Hotovo, když:** režisér vidí všechny telefony a jejich stav a dokáže založit relaci bez nahrávání.

## 2. Lokální záznam videa a telemetrie

- [ ] Inicializovat kameru a nabídnout pouze ověřené kombinace rozlišení/FPS/kodeku.
- [ ] Nahrávat přes `MediaRecorder` po kratších blocích, ne jako jediný obří Blob v RAM.
- [ ] Průběžně ukládat bloky do IndexedDB a obnovit stav po reloadu nebo pádu PWA.
- [ ] Zaznamenávat monotónní čas a jen metadata nezbytná pro synchronizaci záznamu.
- [ ] U časových událostí uložit čas vůči začátku lokálního záznamu; nemíchat bez převodu různé časové zdroje.
- [ ] Zaznamenat skutečná nastavení streamu, typ MIME, rozměry, FPS a verzi aplikace.
- [ ] Ošetřit zamknutí obrazovky, přepnutí aplikace, nedostatek místa a odebrání oprávnění.

**Hotovo, když:** telefon pořídí několikaminutový záznam offline a po restartu aplikace jej stále nabídne k odeslání.

## 3. Řízení a synchronizace

- [ ] Implementovat přes WebSocket povely `ARM`, `START`, `STOP` s potvrzením od každého klienta.
- [ ] Změřit offset a round-trip time hodin mezi telefony a serverem opakovaným handshake.
- [ ] Ukládat plánovaný i skutečný lokální čas startu/stopu; síťový povel nepovažovat za současný okamžik na všech telefonech.
- [ ] Použít dobře viditelný záblesk nebo LED panel jako obrazovou klapku v zorném poli kamer.
- [ ] Přidat volitelnou zvukovou klapku pro záložní synchronizaci.
- [ ] V postprocessingu detekovat klapku ve videích a uložit korekci časové osy.
- [ ] Ověřit omezení ovládání svítilny v cílových mobilních prohlížečích; připravit externí světlo jako spolehlivý fallback.

**Hotovo, když:** pět krátkých záznamů lze po klapce zarovnat na konkrétní snímek a je znám rozptyl synchronizace.

## 4. Spolehlivý přenos na notebook

- [ ] Připravit endpointy pro manifest, chunkovaný upload a dokončení souboru.
- [ ] Ukládat uploady atomicky do adresářů podle relace a zařízení.
- [ ] Podporovat opakování bloků, pokračování přenosu a idempotentní požadavky.
- [ ] Po dokončení ověřit počet bloků, velikost a SHA-256 videa i telemetrie.
- [ ] Server vrátí podepsané/identifikovatelné potvrzení o převzetí.
- [ ] Smazání v telefonu povolit až po potvrzení; výchozí chování má vyžadovat vědomé potvrzení uživatele.
- [ ] Zobrazit průběh, rychlost, zbývající čas a chyby jednotlivých telefonů.

**Hotovo, když:** přerušený přenos pokračuje bez ztráty dat a lokální kopie nezmizí před úspěšnou verifikací.

## 5. Laboratorní a síťové testy

- [ ] Unit testy schémat, stavového automatu, checksumů a opakovaných uploadů.
- [ ] Integrační test celé relace s jedním telefonem, poté s pěti.
- [ ] Test vypnutí Wi-Fi, pádu serveru, reloadu PWA, plného disku a vybití telefonu.
- [ ] Změřit propustnost staršího AP a dobu přenosu reálných souborů.
- [ ] Ověřit současný upload; případně telefony řadit do fronty, aby AP nebyl zahlcen.
- [ ] Ověřit teplotu, throttling a spotřebu baterie při cílové délce záznamu.
- [ ] Archivovat protokol testu a přesné verze zařízení/aplikace.

**Hotovo, když:** simulované výpadky nezpůsobí tiché poškození ani ztrátu jediného záznamu.

## 6. Terénní zkouška

- [ ] Zajistit zařízení proti pádu a vymezit bezpečnou zónu pod balkonem.
- [ ] Natočit krátkou zkušební scénu ze všech připravených úhlů.
- [ ] Ověřit viditelnost obrazové nebo zvukové klapky ve všech záznamech.
- [ ] Před odchodem z místa ověřit úplnost všech manifestů, videí, telemetrie a checksumů.

**Hotovo, když:** všechna videa jsou kompletní, přenesená na notebook a prokazatelně synchronizovatelná.

## 7. Výstup relace

- [ ] Uložit všechna videa do jednoho adresáře relace s jednoznačnými názvy zařízení.
- [ ] Uložit manifest se startovními časy, korekcemi podle klapky, délkami, velikostmi a checksumy.
- [ ] Zachovat původní soubory beze změn; případné synchronizované kopie ukládat odděleně.
- [ ] Vytvořit jednoduchý report úplnosti a výsledného časového posunu každého videa.

**Hotovo, když:** centrální uzel obsahuje úplnou, ověřenou a synchronizovanou sadu videí připravenou pro použití jiným projektem.

## Výslovně mimo scope

- [ ] Kalibrace kamer a mapování prostorové geometrie.
- [ ] GPS/orientační telemetrie, pokud není přímo potřebná k synchronizaci.
- [ ] Detekce objektů a osob, pose estimation a tracking.
- [ ] Rekonstrukce scény, asociační matice a vícekamerová fúze.
- [ ] AI pipeline, ComfyUI, Ollama a optimalizace v C++.

## Terénní checklist dne natáčení

- [ ] Notebook, napájení, dostatek místa, AP/router, dlouhý Ethernet a náhradní kabel.
- [ ] Nabité telefony, držáky, ochrana proti pádu a powerbanky.
- [ ] Funkční lokální HTTPS, načtená PWA a udělená oprávnění.
- [ ] Kontrola času, volného místa, kamery, orientace, GPS a síťového signálu na každém telefonu.
- [ ] Zkušební desetisekundová relace, upload a kontrola přehrání všech videí.
- [ ] Dokumentace označení telefonů a jejich úhlů záběru.
- [ ] Ostrá relace a případně jedna bezpečnostní opakovaná relace.
- [ ] Dvojí kontrola checksumů a záloha na druhé úložiště před smazáním z telefonů.
