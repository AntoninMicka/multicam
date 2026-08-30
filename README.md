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

## Definice úspěchu MVP

- všechna zařízení se připojí k jedné relaci a zobrazí stav připravenosti;
- režisér spustí a zastaví záznam;
- každé zařízení lokálně uloží video a telemetrii se společným ID relace;
- přerušený upload lze obnovit nebo bezpečně zopakovat;
- server ověří velikost a SHA-256 a teprve potom povolí smazání lokální kopie;
- výsledná videa lze časově zarovnat podle obrazové klapky a časových metadat.
