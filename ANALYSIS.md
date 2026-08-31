# Analýza top-down kamery

Analytická část převádí ověřený záznam z kamery s rolí `top_camera` na
referenční časovou osu a trajektorie ve společném souřadném systému scény.
Výsledek je odvozený artefakt: původní video ani telemetrii nikdy nemění a lze
jej opakovaně přepočítat jinou verzí algoritmu.

## Souřadné systémy

- `image_px`: pixely zdrojového videa, počátek vlevo nahoře, `x` doprava a `y` dolů;
- `scene_m`: pravotočivá soustava scény v metrech, `z = 0` je sledovaná rovina;
- `camera`: lokální soustava kamery podle OpenCV (`x` doprava, `y` dolů, `z` vpřed).

Převod bodů z top kamery do roviny scény zajišťuje homografie. Pro každý
kalibrační bod se ukládá pixelová i známá metrická pozice. Měřítko proto nesmí
být odhadováno pouze z rozměrů obrazu.

## Referenční čas

Čas `reference_time_s = 0` odpovídá společné synchronizační události relace.
Čas snímku vznikne z jeho PTS a korekce klapky příslušného záznamu. Zdrojové PTS
i číslo snímku se zachovají kvůli auditovatelnosti. Při nepravidelném FPS se
nesmí čas dopočítávat jako `frame_index / fps`.

## Navržená pipeline

1. Vybrat ověřený záznam `top_camera` a načíst jeho korekci klapky.
2. Dekódovat skutečné PTS jednotlivých snímků (FFmpeg/PyAV).
3. Načíst kalibraci objektivu a roviny scény; obraz nejprve undistortovat.
4. Detekovat objekty a udržovat stabilní `track_id` i při krátkém zakrytí.
5. Reprezentativní bod objektu (typicky střed dolní hrany bboxu) převést
   homografií z `image_px` do `scene_m`.
6. Uložit kalibraci, kamery, stopy, nejistoty a provenance do jednoho
   verzovaného JSON souboru podle `schemas/topdown-analysis.schema.json`.

OpenCV pokrývá kalibraci, korekci objektivu, homografii, markery a klasické
trackery. Samotná identita sledovaných objektů je samostatný vyměnitelný krok:
pro barevné/ArUco markery stačí deterministická OpenCV detekce, pro osoby či
neoznačené objekty bude potřeba detektor a multi-object tracker.

## Kalibrace scény a kamer

Pro rovinnou homografii jsou potřeba nejméně čtyři nekolineární body v rovině
s přesně změřenými souřadnicemi.
Pro lepší kontrolu se doporučuje 6–10 bodů po obvodu scény a uložit reprojekční
chybu. Intrinsiky každého telefonu se kalibrují zvlášť pro konkrétní rozlišení,
zoom a objektiv.

Polohu a orientaci kamer nelze obecně získat pouze z top-down záznamu. Každá
kamera proto dostane extrinsickou kalibraci vůči `scene_m`, například pomocí
viditelných ArUco/ChArUco bodů, nebo ručního geodetického zaměření. Neznámé
hodnoty zůstávají `null`; nesmí se nahrazovat GPS telefonu jako přesnou pozicí.

Pokud je kamera nebo kameraman viditelný v top-down záběru, může být jeho stopa
asociována s konkrétním `device_id`. Jde o dynamickou polohu reprezentativního
bodu na zemi, nikoli automaticky o úplnou 6DoF extrinsickou kalibraci kamery.
Viditelný ArUco marker pevně spojený s kamerou může navíc dodat orientaci.

## Porovnání s GNSS telemetrií

GNSS a obrazový tracking se uchovávají jako dva samostatné zdroje měření. Pro
porovnání se WGS84 převede do lokální tečné soustavy ENU a následně pevným 2D
převodem do `scene_m`. Transformace se odhadne z několika zaměřených bodů nebo
z asociované trajektorie pouze tehdy, když má dostatečný prostorový rozsah.

Každý porovnaný vzorek zachová původní latitude/longitude, deklarovanou GNSS
přesnost, obrazovou pozici, promítnutou GNSS pozici a reziduum v metrech. GNSS
se nebude používat jako přesný ground truth: v běžném telefonu může mít chybu,
zpoždění i nepravidelnou vzorkovací frekvenci. Časy se porovnají na společné
referenční ose a interpolace musí být v JSON označena.

## Výstupy a kvalita

Výchozí cesta artefaktu je
`sessions/<session_id>/analysis/<take_id>/topdown-analysis.json`. Každá pozice
obsahuje confidence a příznak, zda byla přímo detekována, nebo pouze predikována.
Analýza má selhat explicitně, pokud chybí top kamera, synchronizace nebo platná
homografie. První akceptační test použije známé markery v několika změřených
bodech a ověří časovou i prostorovou chybu proti ručnímu ground truth.

## Spuštění na jiném stroji

Relace se přenáší jako kontrolovaný `.multicam.zip` balík popsaný v README a na
zpracovacím stroji se nejprve ověří a rozbalí. Procesor smí zapisovat pouze pod
`analysis/<take_id>/`; zdrojová videa, telemetrie a bundle manifest jsou read-only.

Preferovaným vizuálním orchestrátorem je ComfyUI. Vstupní uzel/adaptér vybere
top-over záznam a dekóduje snímky se skutečnými PTS, workflow provede modelovou
detekci nebo klasifikaci a výstupní adaptér zapíše mezivýsledky. Kalibrace,
homografie, časové zarovnání a validace finálního JSON zůstávají v
deterministickém Python/OpenCV kroku mimo modelové uzly.

Ollama je alternativní nebo doplňkový backend pro vision model nad vybranými
snímky a pro textovou kontrolu výsledků. Každé jeho volání musí v provenance
uložit model, digest/verzi, prompt a parametry. Volný text modelu se nikdy
nepovažuje přímo za trajektorii; před přijetím se převádí do schématu a validuje.
