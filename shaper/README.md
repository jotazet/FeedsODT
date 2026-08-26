# Silniki routujące (dla kroku `shape`)

Konfiguracja dwóch silników, które liczą geometrię przejazdu dla feedów
z `shape = "..."` w [`config/sources.toml`](../config/sources.toml).

| katalog | silnik | dla `route_type` | profil |
|---|---|---|---|
| `openrailrouting/` | [OpenRailRouting](https://github.com/geofabrik/OpenRailRouting) — fork GraphHoppera po torach | 0 (tramwaj), 2 (kolej) | `tram`, `rail` |
| `graphhopper/` | [GraphHopper](https://github.com/graphhopper/graphhopper) | 3 (autobus) | `bus` |

Zwykły GraphHopper routuje wyłącznie po drogach, dlatego pociągi i tramwaje
idą przez fork Geofabrika: zna tory, wie, że pociąg nie skręca pod kątem
prostym, i czyta kolejowe atrybuty OSM (rozstaw szyn, elektryfikacja,
kierunek zasadniczy).

## Pochodzenie

Wszystko w tym katalogu pochodzi z
[gtfs-shaper-maker](https://github.com/jotazet/gtfs-shaper-maker)
(MIT, © 2025 jotazet), rewizja `8a0ef3a`. Licencja: `LICENSE.gtfs-shaper-maker`.
Stamtąd pochodzi też [`feedsodt/shaping/router.py`](../feedsodt/shaping/router.py),
skopiowany bez zmian.

Bez zmian: `Dockerfile`, `entrypoint.sh`, `config-bus.yml`.

### Odstępstwa

**`openrailrouting/config.yml` — profil `rail` ma dołożony `gauge_1435.json`**
(model bundlowany w jarze OpenRailRouting). Bez niego pociągi pasażerskie
potrafią zostać dopasowane do Linii Hutniczej Szerokotorowej — 1520 mm,
biegnącej od granicy z Ukrainą po Sławków, miejscami równolegle do zwykłych
linii. Reguła brzmi `gauge != 0 && gauge != 1435`, więc tory bez otagowanego
rozstawu przechodzą i normalna sieć nie jest wycinana. Profil `tram` zostaje
bez tego modelu, bo w Polsce jeżdżą sieci metrowe (Grudziądz, Elbląg).

**`graphhopper/custom_models/bus_shaper.json` — preferencja dróg głównych.**
W oryginale karana była tylko zabudowa mieszkaniowa (`RESIDENTIAL` × 0,2),
więc zastępczy autobus Olkusz — Bukowno jechał w całości drogami `tertiary`
zamiast DK94. Teraz każda niższa klasa drogi ma własną karę
(`SECONDARY` 0,8 → `TERTIARY` 0,5 → `UNCLASSIFIED` 0,3 → `RESIDENTIAL` 0,15
→ `SERVICE`/`LIVING_STREET` 0,1), a `distance_influence` spadło z 90 na 45,
żeby preferencja miała czym zadziałać. Na sześciu trasach kontrolnych
udział dróg głównych wzrósł z 21% do 33%, kosztem 8% dłuższych przejazdów.

Wartości dobrane pomiarem — model da się testować bez reimportu, wysyłając
go w polu `custom_model` żądania `/route` z `ch.disable=true`.

Trzymamy kopię, a nie zależność, bo gtfs-shaper-maker nie jest paczką
instalowalną (nie ma `pyproject.toml` ani `setup.py`). Żeby odświeżyć:

```bash
git clone --depth 1 https://github.com/jotazet/gtfs-shaper-maker /tmp/gsm
diff -r /tmp/gsm/docker shaper --exclude=cache --exclude=README.md --exclude='LICENSE*'
diff /tmp/gsm/router.py feedsodt/shaping/router.py
```

Własna jest tylko orkiestracja: [`feedsodt/shaping/`](../feedsodt/shaping/)
zamiast `main.py` — bo FeedsODT potrzebuje deterministycznych `shape_id`,
cache'u geometrii między przebiegami i strumieniowego zapisu ZIP-a. Powody
są rozpisane w docstringu tego pakietu.

## Uruchomienie

```bash
docker compose --profile shape up -d
```

Adres wycinka OSM bierze się z `FEEDSODT_OSM_FILE` w `.env` (domyślnie
`./osm/poland-latest.osm.pbf`). Wycinek musi pokrywać obszar feedu — kursy
wychodzące poza niego nie dostaną geometrii i są raportowane w logu
oraz w `index.json`.

```bash
# pobranie wycinka
mkdir -p osm
# -L jest konieczne: Geofabrik odbija 302 na nazwę z datą (poland-260823.osm.pbf).
# Bez niego zapisze się 242-bajtowa strona przekierowania, a silnik wywali się
# przy imporcie. --fail pilnuje, żeby błąd HTTP nie wylądował w pliku jako treść.
curl -L --fail -o osm/poland-latest.osm.pbf https://download.geofabrik.de/europe/poland-latest.osm.pbf

# podgląd importu
docker compose --profile shape logs -f railrouting graphhopper-bus
```

## Zmiana modelu wymaga przeliczenia grafu

GraphHopper zapisuje profile w grafie i odmawia wczytania grafu zbudowanego
z innym modelem — `IllegalStateException: Profiles do not match`. Po każdej
edycji `config.yml` albo `bus_shaper.json` trzeba więc wyczyścić odpowiedni
katalog w `cache/`:

```bash
docker compose --profile shape stop railrouting
rm -rf shaper/cache/rail
docker compose --profile shape up -d railrouting
```

OpenRailRouting tego nie sprawdza i **wstaje po cichu ze starym grafem** —
zmiana wygląda na wprowadzoną, a nic nie robi. Dlatego `config.yml` jest
montowany z hosta (mimo że upstream wpieka go w obraz): inaczej trzeba by
pamiętać jeszcze o `--build`, a i to nie dałoby żadnego sygnału w logach.
Po każdej zmianie modelu warto sprawdzić na żywo, czy weszła — testy
jednostkowe nie dotykają silników:

```bash
curl -s -X POST localhost:45889/route -H 'Content-Type: application/json' -d \
  '{"profile":"rail","points":[[21.0594,50.52911],[21.10634,50.5241]],
    "points_encoded":false,"details":["gauge"]}' | grep -o '"gauge".*'
```

Oba punkty leżą na LHS. Poprawny wynik zawiera wyłącznie `1435`.

## Grafy w `cache/`

`cache/rail` i `cache/bus` to zaimportowane grafy routingu. Przeżywają
restarty kontenerów, więc import dzieje się raz — dla wycinka
ogólnopolskiego 2 min (kolej, 51 MB grafu) i 2,5 min (autobus, 591 MB).

**Grafy w tym repozytorium zostały zaimportowane 24.08.2026** z wycinka
`osm/poland-latest.osm.pbf`, po dołożeniu filtra rozstawu i preferencji dróg
głównych. Import zajął 2 min (kolej) i 2,5 min (autobus). Żeby przeliczyć je
od zera:

```bash
docker compose --profile shape down
rm -rf shaper/cache/rail shaper/cache/bus
docker compose --profile shape up -d      # import, ok. 5 minut łącznie
```

## Na maszynie poniżej 8 GB nie importuj — skopiuj graf

To jest najważniejsza rzecz w tym pliku. Import i praca to dwa zupełnie różne
budżety pamięci:

| | kolej | autobus | razem |
|---|---|---|---|
| **import** grafu ogólnopolskiego | ~1 GB | **do 2,6 GB** | ponad 3 GB |
| **praca** na gotowym grafie | 53–70 MB | 835–875 MB | **ok. 1 GB** |

Na VPS-ie z 4 GB import autobusowego grafu wywróci maszynę — JVM zajmie
wszystko, co jest, system wejdzie w swap i przestanie odpowiadać. Dlatego
kontenery mają twarde `mem_limit` (1200 MB i 1800 MB): przekroczenie ubija
proces w kontenerze, zamiast dławić hosta. Pod tymi limitami import **się nie
uda i tak ma być**.

Na małej maszynie graf się przenosi, nie buduje:

```bash
# na maszynie z zapasem pamięci (8 GB+)
docker compose --profile shape down
rm -rf shaper/cache/rail shaper/cache/bus
RAIL_MEM_LIMIT=4g BUS_MEM_LIMIT=6g RAIL_JAVA_OPTS="-Xmx3g" BUS_JAVA_OPTS="-Xmx5g" \
  docker compose --profile shape up -d

# potem 641 MB na serwer docelowy
rsync -a shaper/cache/ rocky@serwer:/sciezka/FeedsODT/shaper/cache/
```

Plik `.osm.pbf` na serwerze docelowym i tak musi być — entrypoint sprawdza jego
obecność — ale nikt go już nie czyta, dopóki katalog grafu jest niepusty.

Wartości nadpisuje się przez `RAIL_JAVA_OPTS` / `BUS_JAVA_OPTS` i
`RAIL_MEM_LIMIT` / `BUS_MEM_LIMIT` w `.env`.

## Konflikt portów

Silniki słuchają na `45889` (kolej) i `45890` (autobus) — tych samych, których
używa własne `docker-compose.yml` gtfs-shaper-makera. Dwóch kompletów naraz
nie da się uruchomić i nie ma po co: to te same silniki, robiące to samo.
