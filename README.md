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

Skript podle potřeby vytvoří `.venv`, nainstaluje Python a npm závislosti, sestaví PWA a spustí FastAPI server. Výchozí adresa je `http://0.0.0.0:8000`; nastavení lze změnit například takto:

```bash
MULTICAM_HOST=127.0.0.1 MULTICAM_PORT=9000 ./run.sh
```

Další argumenty se předají přímo Uvicornu, například `./run.sh --reload`.

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

REST API dokumentace je za běhu dostupná na `http://localhost:8000/docs`. Manifest relace a časové události mají verzovaná schémata v adresáři [`schemas`](schemas).

## Aktuální stav

Fáze 1 obsahuje in-memory správu relací a zařízení. Restart serveru proto relace smaže; perzistentní úložiště bude doplněno spolu se záznamovou a uploadovací částí.

## Definice úspěchu MVP

- všechna zařízení se připojí k jedné relaci a zobrazí stav připravenosti;
- režisér spustí a zastaví záznam;
- každé zařízení lokálně uloží video a telemetrii se společným ID relace;
- přerušený upload lze obnovit nebo bezpečně zopakovat;
- server ověří velikost a SHA-256 a teprve potom povolí smazání lokální kopie;
- výsledná videa lze časově zarovnat podle obrazové klapky a časových metadat.
