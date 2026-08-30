# MultiCam PWA Capture

Lokální systém pro provizorní synchronizovaný záznam videa z několika telefonů. Telefony nahrávají offline do lokálního úložiště, notebook v dočasné Wi-Fi síti zajišťuje řízení relace, příjem dat, kontrolu integrity a společné uložení.

## Cíl první verze

Spolehlivě pořídit z několika telefonů jednu společnou relaci obsahující videa a pouze metadata nutná k jejich synchronizaci, přenést ji na notebook a data na telefonech odstranit až po ověření kontrolního součtu.

## Navržený stack

- mobilní PWA: Vue 3 + Vite + TypeScript + `vite-plugin-pwa`
- lokální server: Python + FastAPI + WebSocket
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

Při prvním spuštění vznikne lokální certifikační autorita a serverový certifikát v ignorovaném adresáři `certs/`. Soubor `certs/local-ca.cert.pem` je potřeba nainstalovat jako důvěryhodnou CA do každého telefonu. Privátní soubor `certs/local-ca.key.pem` se nesmí kopírovat ani zveřejňovat.

Serverový certifikát automaticky zahrnuje `localhost`, `127.0.0.1` a LAN adresy zjištěné při jeho vytvoření. Pokud je automatické zjištění nedostupné nebo se IP notebooku změnila, certifikát obnovte explicitně:

```bash
MULTICAM_CERT_IPS=192.168.1.10 ./scripts/generate-local-cert.sh --force
```

Více adres lze oddělit čárkou. Pro jednorázové vývojové spuštění bez TLS lze použít `MULTICAM_HTTPS=0 ./run.sh`.

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

Spuštění a zastavení záznamu lze ovládat z režisérského pultu i z hlavní kamery. Povel se přenese na všechny připojené kamery. Po zastavení klient vypočítá SHA-256 a automaticky odešle záznam ve 4MB blocích. Server podporuje opakované bloky a navázání podle již přijatých částí, výsledný soubor skládá atomicky a označí zařízení jako `verified` až po kontrole velikosti a SHA-256. Data se ukládají do `data/sessions/`; umístění lze změnit proměnnou `MULTICAM_DATA_DIR`.

Ke každému videu klient vytváří párovaný telemetrický soubor JSON Lines s monotónními a UTC časy startu, stopu, pravidelných vzorků hodin a přijetí synchronizační klapky. Každý vzorek obsahuje také poslední GNSS pozici, orientaci zařízení a skutečné nastavení zoomu kamerového tracku. Webová API neposkytují spolehlivě fyzický úhel záběru, proto je `field_of_view_deg` bez kalibrace telefonu explicitně `null`; `zoom_ratio`, rozlišení a ostatní dostupné hodnoty se ukládají přímo. Zařízení přejde do stavu `verified` až po ověření videa i telemetrie.

Dvě sekundy po povelu ke spuštění záznamu server automaticky vyšle světelnou klapku. Hlavní kamera zkusí přes `MediaStreamTrack.applyConstraints()` zapnout hardwarovou svítilnu (`torch`), což je určeno především pro Chrome na Androidu. Pokud prohlížeč nebo telefon tuto možnost neposkytne, použije se celoobrazovkový bílý záblesk.

Každý blok z `MediaRecorder` a každá telemetrická událost se průběžně ukládají do IndexedDB. Po reloadu aplikace zobrazí dokončené i přerušené lokální záznamy a dovolí jejich upload zopakovat. Ověřenou lokální kopii lze smazat pouze samostatným tlačítkem a po výslovném potvrzení uživatele.

REST API dokumentace je za běhu dostupná na `https://localhost:8000/docs`. Manifest relace a časové události mají verzovaná schémata v adresáři [`schemas`](schemas).

Režisérský pult nabízí seznam uložených relací. Po výběru zobrazí matici všech ověřených kamerových záznamů. Každý přehrávač načte párovanou telemetrii a podle aktuálního času videa zobrazuje nejbližší číselný vzorek: zoom, GNSS souřadnice, přesnost a orientační úhly. Manifesty relací se ukládají jako `data/sessions/<session_id>/session.json`, takže seznam přežije restart serveru.

## Aktuální stav

Fáze 1 obsahuje in-memory správu relací a zařízení. Restart serveru proto relace smaže; perzistentní úložiště bude doplněno spolu se záznamovou a uploadovací částí.

## Definice úspěchu MVP

- všechna zařízení se připojí k jedné relaci a zobrazí stav připravenosti;
- režisér spustí a zastaví záznam;
- každé zařízení lokálně uloží video a telemetrii se společným ID relace;
- přerušený upload lze obnovit nebo bezpečně zopakovat;
- server ověří velikost a SHA-256 a teprve potom povolí smazání lokální kopie;
- výsledná videa lze časově zarovnat podle obrazové klapky a časových metadat.
