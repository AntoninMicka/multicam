# MultiCam na Turris Omnia se SSD

Připravený profil používá **Debian 13 (armhf) v LXC**, frontend sestavený na notebooku a jeden proces Uvicornu. Omnia je rovnocenný backend; v nastavení federace jí přidělte roli **Storage**, director může zůstat na notebooku. Router ukládá originály a ve výchozím nastavení je nepřekóduje (`MULTICAM_TRANSCODE=0`).

Tento postup je připravený pro nasazení, nikoli potvrzení testu na konkrétním routeru. Debian 13 musí být kompatibilní s jádrem používané verze Turris OS; nejprve ověřte start kontejneru. Turris dokumentuje [provoz LXC na externím disku](https://docs.turris.cz/geek/lxc/lxc/) a [nastavení úložiště](https://docs.turris.cz/basics/foris/storage-plugin/storage-plugin/). Debian 13 má balíčky [FastAPI](https://packages.debian.org/trixie/python3-fastapi) a [Pydantic 2 pro armhf](https://packages.debian.org/trixie/python3-pydantic), takže se na routeru nemusí kompilovat Rust ani instalovat Node.js.

## 1. Ověření SSD na hostiteli Turris OS

Připojte SSD a nakonfigurujte jeho trvalé připojení podle UUID, například do `/srv`. Tento postup disk **neformátuje**. Jak kořen kontejneru LXC, tak data MultiCamu musí ležet na SSD. Nepoužívejte interní eMMC. Z notebooku přeneste `check-host-ssd.sh` do `/tmp` routeru a spusťte:

```sh
sh /tmp/check-host-ssd.sh /srv
```

Kontrola vyžaduje `findmnt`, `mountpoint` a dostupné `/sys`. Přijme pouze samostatně připojené zapisovatelné blokové zařízení `/dev/sd*` nebo `/dev/nvme*`, které hlásí `rotational=0`; eMMC odmítne. USB flash může také hlásit `rotational=0`, proto fyzicky ověřte, že jde o zamýšlené SSD.

Po úspěšné kontrole vytvořte adresář a jedinečný identifikátor SSD:

```sh
mkdir -p /srv/multicam
[ -f /srv/multicam/.multicam-storage-id ] || cat /proc/sys/kernel/random/uuid > /srv/multicam/.multicam-storage-id
cat /srv/multicam/.multicam-storage-id
```

Identifikátor si poznamenejte. Nekopírujte jej na interní flash jako náhradu SSD.

## 2. LXC na SSD

V reForis nainstalujte LXC utilities a vytvořte Debian 13 armhf kontejner `multicam` podle dokumentace Turrisu. Uložte jej na SSD. Dejte mu vlastní stálou LAN IP (např. `192.168.1.20`). Do konfigurace zastaveného kontejneru přidejte povinný bind mount:

```ini
lxc.mount.entry = /srv/multicam srv/multicam-ssd none bind,create=dir 0 0
```

Nepřidávejte `optional`. Automatický start kontejneru nastavte až po ověření mountu; bez SSD se nemá spustit ani LXC. Uvnitř kontejneru musí `/srv/multicam-ssd` obsahovat `.multicam-storage-id` a být samostatným mountem. Při unprivileged LXC přizpůsobte vlastnictví adresářů mapování UID.

## 3. Sestavení a přenos aplikace

Na notebooku v repozitáři:

```sh
./scripts/build-omnia-release.sh /tmp/multicam-omnia.tar.gz
scp /tmp/multicam-omnia.tar.gz root@192.168.1.20:/srv/multicam-ssd/
```

Archiv obsahuje pouze backend, sestavený frontend a instalační soubory, žádná videa, certifikáty ani tokeny. Uvnitř LXC:

```sh
mkdir -p /srv/multicam-ssd/app
cd /srv/multicam-ssd/app
tar -xzf /srv/multicam-ssd/multicam-omnia.tar.gz
cp deploy/omnia/multicam.env.example /etc/multicam.env
```

V `/etc/multicam.env` vyplňte skutečnou LAN URL a `MULTICAM_STORAGE_ID` podle SSD. Nahrajte TLS certifikát platný pro adresu kontejneru a klíč do `/srv/multicam-ssd/certs/`; jejich cesty jsou v konfiguraci. Kamerové prohlížeče musí certifikátu důvěřovat.

```sh
apt-get update
apt-get install -y python3
sh deploy/omnia/install-container.sh
systemctl enable --now multicam
systemctl status multicam
journalctl -u multicam -n 50
```

Instalátor používá distribuční Python balíčky, nikoli notebookové `.venv`. Při aktualizaci nejprve zastavte službu, rozbalte nový archiv a službu znovu spusťte; adresáře `data/` a `certs/` se nepřepisují.

## 4. Spárování a určení rolí

Povolte TCP 8000 mezi backendy a klienty v LAN, případně UDP 47777 pro discovery. Nepotřebujete publikovat port do WAN. Bez multicastu funguje celý párovací odkaz a uložená unicast adresa.

Změny párování a rolí jsou dostupné pouze přes localhost daného backendu. Pro headless Omnii použijte SSH tunel:

```sh
ssh -L 18000:127.0.0.1:8000 root@192.168.1.20
```

Otevřete `https://localhost:18000` (certifikát musí pokrýt i localhost). V panelu **Spárovat pulty / nastavení federace** vložte celý odkaz vytvořený na notebooku. Před párováním nesmí mít připojovaný uzel vlastní aktuální relaci. Pak nastavte **Director = notebook**, **Storage = Omnia**. Obě role mohou později převzít jiné spárované uzly; předání directora potvrzuje dosavadní director mimo nahrávání. Automatické převzetí při síťovém výpadku se neprovádí.

## 5. Kontrola před používáním

- S připojeným SSD musí `GET /api/health` vracet 200 a všechny backendy zobrazovat stejnou aktuální relaci.
- Natočte krátký záběr na kameře připojené k notebooku. Po uploadu musí fronta přenosu klesnout na nulu a originál s telemetrií být v archivu Omnie.
- Ukončete relaci a smažte její kopii na notebooku. Archiv Omnie musí zůstat zachovaný.
- Zastavte službu i kontejner, bezpečně odpojte SSD a ověřte, že kontejner/služba nenastartuje. Disk neodpojujte během zápisu.

Služba vyžaduje mount SSD a správný identifikátor. Backend kontroluje mount i skutečnou možnost zápisu před inicializací a při HTTP požadavcích i zápisech stavu. Při nedostupnosti vrací 503 / odmítne start; nemá náhradní datový adresář na flash. Dočasné ZIPy a pracovní soubory jsou také na SSD. Rozpracované přenosy zůstávají na odesílajících backendech a po obnovení storage se opakují. Router originály pouze ověřeně ukládá; dostupnost SSD nenahrazuje další zálohu.

## Aktualizace s rollbackem

Na notebooku znovu sestavte release a přeneste jej na SSD. Uvnitř LXC po ukončení aktivní relace spusťte:

```sh
sh /srv/multicam-ssd/app/deploy/omnia/update-container.sh /srv/multicam-ssd/multicam-omnia.tar.gz
```

Skript před zastavením služby ověří SSD, bezpečný obsah archivu a syntaxi Pythonu. Uchová předchozí aplikaci, aktualizuje službu a závislosti a počká na zdravý backend. Při chybě instalace/startu vrátí předchozí aplikaci; data, identita backendu, párování, IP kamery a TLS konfigurace leží mimo adresář aplikace. Rollback aplikace nevrací databázová data ani balíčky Debianu; aktualizujte proto až mimo nahrávání. Zálohy aplikace odstraňujte až po ověření nové verze.
