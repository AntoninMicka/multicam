# Roadmapa

Stavy: `[x]` implementováno, `[~]` implementováno
částečně nebo čeká na ověření na cílových zařízeních, `[ ]` dosud není hotovo.

## 0. Rozhodnutí a ověření zařízení

- [ ] Sepsat cílové telefony, OS, prohlížeče, rozlišení, kodeky a volné místo.
- [ ] Ověřit `getUserMedia`, `MediaRecorder`, IndexedDB, GPS a `DeviceOrientation` na každém telefonu.
- [ ] Ověřit oprávnění senzorů, zejména explicitní souhlas na iOS.
- [ ] Zvolit podporovaný profil videa podle nejslabšího telefonu; nezačínat automaticky ve 4K.
- [~] Připravit lokální HTTPS (generování CA a serverového certifikátu je hotové; zbývá ověřit instalaci a důvěru na všech cílových telefonech).
- [ ] Změřit dostupné místo a odhadnout velikost jedné relace s bezpečnostní rezervou.

**Hotovo, když:** všech pět cílových telefonů projde krátkým kompatibilitním testem a známe společný formát záznamu.

## 1. Kostra systému a relace

- [x] Založit Vue 3/Vite/TypeScript PWA s manifestem a offline app shellem.
- [x] Založit FastAPI server pro statické soubory, REST API a WebSocket.
- [x] Zavést role `director` a `camera` a registraci zařízení.
- [x] Zavést `session_id`, `device_id`, stavový automat (`disconnected`, `ready`, `recording`, `stored`, `uploading`, `verified`).
- [x] V režisérském UI zobrazit připojená zařízení, baterii, místo, oprávnění a připravenost.
- [x] Definovat verzované JSON schéma manifestu relace a telemetrie.

**Hotovo, když:** režisér vidí všechny telefony a jejich stav a dokáže založit relaci bez nahrávání.

## 2. Lokální záznam videa a telemetrie

- [~] Inicializovat kameru a nabídnout pouze ověřené kombinace rozlišení/FPS/kodeku (kamera se inicializuje a MIME se vybírá podle podpory prohlížeče, chybí uživatelská volba a matice ověřených profilů).
- [x] Nahrávat přes `MediaRecorder` po kratších blocích, ne jako jediný obří Blob v RAM.
- [x] Průběžně ukládat bloky do IndexedDB a obnovit stav po reloadu nebo pádu PWA.
- [x] Zaznamenávat monotónní čas a jen metadata nezbytná pro synchronizaci záznamu.
- [x] U časových událostí uložit čas vůči začátku lokálního záznamu; nemíchat bez převodu různé časové zdroje.
- [x] Zaznamenat skutečná nastavení streamu, typ MIME, rozměry, FPS a verzi aplikace.
- [~] Ošetřit zamknutí obrazovky, přepnutí aplikace, nedostatek místa a odebrání oprávnění (Wake Lock, varování při skrytí, kontrola 500 MB před ARM a ztráta mediální stopy jsou hotové; zbývá test plného disku a změn oprávnění na cílových telefonech).

**Hotovo, když:** telefon pořídí několikaminutový záznam offline a po restartu aplikace jej stále nabídne k odeslání.

## 3. Řízení a synchronizace

- [x] Implementovat přes WebSocket povely `ARM`, `START`, `STOP` s potvrzením od každého klienta, chybovým stavem a timeoutem; server odmítne `START`, dokud všechny připojené kamery nepotvrdí ARM.
- [x] Měřit offset a round-trip time hodin mezi telefony a serverem opakovaným handshake; hodnoty zobrazovat na pultu a ukládat do telemetrie záznamu.
- [~] Ukládat plánovaný i skutečný lokální čas startu/stopu (ukládá se čas přijetí povelu, lokální monotónní i UTC čas; chybí plánovaný start a síťová korekce).
- [~] Použít dobře viditelný záblesk nebo LED panel jako obrazovou klapku (automatická klapka po 2 s, hardwarová svítilna hlavní kamery a obrazovkový fallback jsou implementované; zbývá terénní ověření).
- [ ] Přidat volitelnou zvukovou klapku pro záložní synchronizaci.
- [ ] V postprocessingu detekovat klapku ve videích a uložit korekci časové osy.
- [~] Ověřit omezení ovládání svítilny v cílových mobilních prohlížečích (Chrome/Android je podporován experimentálně; externí světlo a testovací matice chybí).
- [x] Označit všechny kamerové záznamy ze stejného startu/klapky společným `take_id` a seskupit je na režisérském pultu.
- [~] Přehrávat skupinu kamer jedním tlačítkem se souběžnou telemetrií (master čas je zarovnaný podle `sync_marker`, drift se koriguje a každý dekodér ukazuje stav; zbývá ověření Chromia na cílovém stroji).

**Hotovo, když:** pět krátkých záznamů lze po klapce zarovnat na konkrétní snímek a je znám rozptyl synchronizace.

## 4. Spolehlivý přenos na notebook

- [x] Připravit endpointy pro manifest, chunkovaný upload a dokončení souboru.
- [x] Ukládat uploady atomicky do adresářů podle relace a zařízení.
- [x] Podporovat opakování bloků, pokračování přenosu a idempotentní požadavky.
- [x] Po dokončení ověřit počet bloků, velikost a SHA-256 videa i telemetrie.
- [x] Server vrátí podepsané/identifikovatelné potvrzení o převzetí.
- [x] Smazání v telefonu povolit až po potvrzení; výchozí chování má vyžadovat vědomé potvrzení uživatele.
- [~] Zobrazit průběh, rychlost, zbývající čas a chyby jednotlivých telefonů (procenta a základní chyby jsou hotové, chybí rychlost a odhad času).

**Hotovo, když:** přerušený přenos pokračuje bez ztráty dat a lokální kopie nezmizí před úspěšnou verifikací.

## 5. Laboratorní a síťové testy

- [~] Unit testy schémat, stavového automatu, checksumů a opakovaných uploadů (existuje 6 backendových API testů včetně idempotentního uploadu a obnovy relace; pokrytí stavů a frontend chybí).
- [ ] Integrační test celé relace s jedním telefonem, poté s pěti.
- [ ] Test vypnutí Wi-Fi, pádu serveru, reloadu PWA, plného disku a vybití telefonu.
- [ ] Změřit propustnost staršího AP a dobu přenosu reálných souborů.
- [~] Ověřit současný upload; klienti nahrávají souběžně, ale chybí měření a případná fronta.
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

- [~] Uložit všechna videa pod jeden adresář relace a zařízení (struktura existuje, názvy souborů zatím nejsou lidsky čitelné).
- [~] Uložit manifest se startovními časy, korekcemi podle klapky, délkami, velikostmi a checksumy (perzistentní manifest relace a metadata uploadů existují, souhrnný výstupní manifest ne).
- [x] Zachovat původní soubory beze změn; synchronizované kopie se zatím nevytvářejí.
- [~] Vytvořit jednoduchý report úplnosti a výsledného časového posunu každého videa (JSON report po klapkách obsahuje očekávané/chybějící kamery, soubory, velikosti a SHA-256; chybí výsledná korekce z detekce obrazu).

**Hotovo, když:** centrální uzel obsahuje úplnou, ověřenou a synchronizovanou sadu videí připravenou pro použití jiným projektem.

## Výslovně mimo scope

- [ ] Kalibrace kamer a mapování prostorové geometrie.
- [x] GPS/orientační telemetrie byla na přání zahrnuta: GNSS, orientace a dostupný zoom se ukládají a zobrazují synchronně s videem.
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

## Doporučené další práce

1. **Ověření nové ochrany a přehrávače na reálných zařízeních.** Prověřit Wake Lock,
   plné úložiště, skrytí PWA a skupinové přehrávání zejména v desktopovém Chromiu.
2. **Integrační zkouška na reálných telefonech.** Nejdřív dvě kamery, pak cílový počet;
   otestovat restart, výpadek Wi-Fi, paralelní upload, teplotu a několikaminutový záznam.
3. **Detekce obrazové nebo zvukové klapky.** Změřit přesnou korekci každého videa,
   uložit ji do reportu a porovnat ji se síťovým odhadem offsetu.
