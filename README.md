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

Navazující scope zahrnuje kalibraci roviny scény a OpenCV analýzu top-down
kamery: společnou referenční časovou osu, polohy sledovaných objektů a
kalibrované pozice kamer ve verzovaném JSON výstupu. Rekonstrukce obecné 3D
scény, pose estimation, vícekamerová fúze a generativní AI zůstávají mimo scope.

## Dokumentace

- [ROADMAP.md](ROADMAP.md) – fáze vývoje, akceptační kritéria a terénní checklist
- [ARCHITECTURE.md](ARCHITECTURE.md) – komponenty, datový tok a zásadní technická omezení
- [ANALYSIS.md](ANALYSIS.md) – souřadné systémy, kalibrace a výstup top-down analýzy

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
Pouze v režisérském pohledu je navíc panel **QR frontendu podle rozhraní**.
Backend v něm vypíše všechny aktivní ne-loopback IPv4 a IPv6 adresy (typicky
Ethernet, Wi‑Fi, hotspot a ZeroTier) a pro každou vytvoří samostatný QR odkaz na
frontend. Po připojení nového rozhraní lze seznam obnovit bez restartu serveru.
QR panely jsou ve výchozím stavu sbalené. Na telefonu/tabletu úvodní obrazovka
nabízí pouze hlavní, top-down a vedlejší kameru; na desktopu pouze režisérský
pult.

### Dva režisérské pulty přes ZeroTier

Každý backend vysílá malý UDP discovery heartbeat a nalezené backendy zveřejní
na `GET /api/backends`. V jedné LAN není potřeba žádná konfigurace. Pro dva
notebooky v různých sítích vytvořte privátní ZeroTier síť a na **obou** strojích
spusťte (16znakové ID nahraďte vlastním):

```bash
sudo ./scripts/setup-zerotier.sh --install 8056c2e21c000001
```

Oba nové členy je následně nutné autorizovat v ZeroTier Central. Zjistěte jejich
ZeroTier IPv4 adresu pomocí `ip -4 addr` a každý backend spusťte například takto:

```bash
MULTICAM_ZEROTIER_NETWORK=8056c2e21c000001 \
MULTICAM_BACKEND_NAME="Pult A" \
MULTICAM_DISCOVERY_INTERFACE_IP=10.147.17.2 \
MULTICAM_PUBLIC_URL=https://10.147.17.2:8000 \
MULTICAM_CERT_IPS=10.147.17.2 \
MULTICAM_FEDERATION_TOKEN="stejny-dlouhy-nahodny-token-na-obou-pultech" \
MULTICAM_FEDERATION_TLS_VERIFY=0 ./run.sh
```

Na druhém notebooku použijte jeho adresu a název `Pult B`. Volba
`MULTICAM_DISCOVERY_INTERFACE_IP` směruje multicast přes ZeroTier místo výchozí
Wi‑Fi. Pokud je multicast v pravidlech ZeroTier sítě zakázaný, nastavte na obou
strojích také `MULTICAM_DISCOVERY_PEERS` na ZeroTier IP druhého notebooku;
discovery pak používá unicast heartbeat. Povolit je potřeba UDP 47777 mezi
notebooky a TCP 8000 pro web/API. Certifikační autoritě druhého pultu je nutné
důvěřovat, pokud se jeho web otevírá přímo v prohlížeči.

Se stejnou hodnotou `MULTICAM_FEDERATION_TOKEN` fungují oba backendy jako jeden
pult: relace a seznam připojených kamer se slučují, `ARM`, `START` a `STOP` se
předají druhému backendu ihned a oba použijí stejné `take_id`. Telefon vždy
odesílá video jen svému lokálnímu notebooku. Teprve až tento notebook ověří
video i telemetrii všech svých telefonů, odešle kontrolovaně zabalenou lokální
část klapky druhému notebooku. Import kontroluje velikosti a SHA-256 a je
idempotentní.

Federace je autoritativní: pult, který vytvoří párovací QR/kód, se stane
`leader`, připojený pult `follower`. Leader jako jediný vytváří, maže a přepíná
aktivní relaci; follower tento stav automaticky převezme. Řídicí povely z
followeru (včetně povelu z hlavní kamery) procházejí přes leader. V celé
federaci smí být právě jedna hlavní a jedna top-down kamera, nezávisle na tom,
ke kterému notebooku jsou připojené. Živé náhledy se mezi backendy neposílají a
zůstávají jen na pultu příslušného telefonu.
U každé kamery i pořízeného streamu UI zobrazuje název backendu, ke kterému je
telefon připojený. Follower vždy načítá své lokálně ověřené záznamy přímo z
vlastního disku a zobrazí je okamžitě, ještě před replikací na leadera.
Follower při párování a následně při každém reconnectu registruje u leadera
svou dosažitelnou adresu. Řídicí povely proto nejsou závislé na obousměrném
multicast discovery přes ZeroTier.

Po ověření lokálních uploadů posílá follower svou část klapky leaderu s trvalým
retry stavem. Opačný směr je vypnutý, dokud leader v UI nezapne **Zálohu na
follower**. Potlačení federačních přenosů zastaví oba směry, nikoli řídicí
povely ani synchronizaci relací.
Tlačítko **Odložit páteřní přenosy** nezahazuje žádná data: telefon vždy nejprve
dokončí upload na přímo připojený backend, ověří se video i telemetrie a hotová
klapka zůstane v trvalé federační frontě. UI ukazuje počet čekajících přenosů.
Po volbě **Spustit odložené přenosy** retry smyčka frontu automaticky odešle;
primárně follower → leader, případně sekundárně leader → follower jako zálohu.

U nalezeného pultu se zobrazuje jeho aktivní relace a tlačítko pro připojení
lokálního pultu. Relace lze odstranit ze seznamu; po potvrzení se smažou její
manifesty, záznamy a odvozené soubory na obou dostupných federovaných pultech.
Běžící relaci backend smazat odmítne.

Token není nutné zadávat ručně. Na prvním notebooku otevřete pult přes lokální
adresu (`https://localhost:8000`), rozbalte **Spárovat pulty / nastavení
federace** a zvolte **Vytvořit párovací QR**. Na druhém lokálně otevřeném pultu
ve stejné sekci QR vyfoťte/načtěte, případně vložte jeho text. Jednorázový kód
je zobrazený také pod QR ve formátu `XXXXX-XXXXX`; na druhém pultu lze zadat
jen tento kód a backend automaticky osloví pulty nalezené přes discovery. Kód
platí pět minut; druhý backend si přes něj převezme token a oba jej uloží do
`data/federation.json` s oprávněním pouze pro vlastníka. V UI lze také bez
restartu povolit nebo potlačit následnou replikaci záznamů.
Protože každý pult standardně používá vlastní lokální CA, QR handshake současně
uloží režim TLS pro toto spojení; autentizaci dalších požadavků zajišťuje
sdílený náhodný token uvnitř privátní ZeroTier sítě. UI zobrazuje čas poslední
úspěšné synchronizace a případnou síťovou/TLS chybu.

Přenos velkých souborů mezi pulty lze vypnout, aniž se vypne okamžité řízení:

```bash
MULTICAM_FEDERATION_TRANSFER=0
```

Ukázka používá `MULTICAM_FEDERATION_TLS_VERIFY=0`, protože každý notebook má
ve výchozím stavu vlastní lokální CA; provoz je stále omezen sdíleným tokenem a
privátní ZeroTier sítí. Bezpečnější varianta je importovat CA druhého pultu a
nastavit její cestu v `MULTICAM_FEDERATION_CA` místo vypnutí ověřování TLS.

ZeroTier je volitelná systémová závislost. Setup skript podporuje Debian a
Ubuntu, kontroluje distribuci, doinstaluje `ca-certificates`, `curl`, `gpg` a
oficiální balík `zerotier-one`, zapne službu a připojí síť. Bez parametru
`--install` chybějící instalaci pouze ohlásí. Discovery lze úplně vypnout přes
`MULTICAM_DISCOVERY=0`.

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

Dvě sekundy po povelu ke spuštění záznamu server automaticky spustí identifikační světelnou klapku: úvodní synchronizační záblesk hlavní kamery, samostatný záblesk každé vedlejší kamery a závěrečný dvojitý podpis hlavní kamery. Top-over kamera sekvenci pouze pozoruje a sama nikdy nebliká. Každý krok se uloží do telemetrie všech kamer a do společného `events.jsonl` relace. Cílová kamera zkusí přes `MediaStreamTrack.applyConstraints()` zapnout hardwarovou svítilnu (`torch`), což je určeno především pro Chrome na Androidu. Pokud prohlížeč nebo telefon tuto možnost neposkytne, použije se celoobrazovkový bílý záblesk.

## Přenos relace na zpracovací stroj

Celou relaci lze bez změny původních videí zabalit do jednoho ZIP64 souboru s
příponou `.multicam.zip`. Balík obsahuje manifest a SHA-256 každého souboru;
vynechává pouze obnovitelné playback proxy a již složené uploadové chunky.

```bash
python -m backend.app.bundle --data-dir data/sessions export SESSION_UUID session.multicam.zip
python -m backend.app.bundle --data-dir /data/multicam import session.multicam.zip
```

Import nejprve ověří seznam, velikosti a checksumy do dočasného adresáře a
teprve potom relaci atomicky zpřístupní. Existující relaci stejného UUID nikdy
nepřepíše. Po importu stačí zpracovací server restartovat, aby relaci načetl.
Jednotlivé klapky lze stáhnout stejným formátem přímo tlačítkem u skupiny
záznamů. Dílčí balík má v manifestu `scope: "take"` a je určený ke zpracování,
nikoli k importu jako úplná relace.

U každé klapky lze také vyrenderovat jedno MP4 s automatickou maticí záběrů.
Pořadí je hlavní kamera, top-over a vedlejší kamery. Pokud byla obrazová klapka
analyzována, vstupy se oříznou na její synchronizační bod; jinak začínají od
počátku souboru. Zvuk se přebírá z hlavní kamery a originály se nemění.

Pro přesné CV kroky je primární deterministická OpenCV/PyAV pipeline. ComfyUI
je preferované orchestrační a vizuální rozhraní pro detekční/modelové workflow;
balík pro něj zůstává adresářovou sadou videí a JSON. Ollama může nad
vybranými snímky a strukturovanými výsledky dělat klasifikaci, popis a kontrolu,
ale nemá nahrazovat dekódování PTS, kalibraci ani numerický tracking.

### Lokální vision analýza top-over

Tlačítko **Analyzovat top-down** u klapky založí asynchronní job. Režim
`prepare` pouze vyextrahuje časované JPEG vzorky; `ollama` je odešle do lokálního
vision modelu se strukturovaným JSON schématem; `comfyui` je nahraje a spustí
nad každým snímkem nakonfigurovaný API workflow. Výsledky, úplné odpovědi a
provenance se ukládají do
`analysis/<take_id>/vision/<job_id>/`.

```bash
MULTICAM_OLLAMA_URL=http://127.0.0.1:11434 \
MULTICAM_OLLAMA_MODEL=gemma3:12b ./run.sh

MULTICAM_COMFY_URL=http://127.0.0.1:8188 \
MULTICAM_COMFY_WORKFLOW=/absolutni/cesta/workflow-api.json ./run.sh
```

ComfyUI workflow musí být exportovaný v API formátu a na místě názvu vstupního
souboru obsahovat přesný řetězec `{{INPUT_IMAGE}}`. Job čeká na dokončení promptu
a uloží také vrácenou historii uzlů. Ve výchozím nastavení jsou z bezpečnostních
důvodů povolené jen loopback HTTP adresy. Pro samostatný stroj v důvěryhodné
lokální síti lze explicitně nastavit `MULTICAM_ALLOW_REMOTE_ANALYSIS=1`.

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
