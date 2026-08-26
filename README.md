# FeedsODT

Pobiera rozkłady GTFS z portali, które nie mają stałych linków, i wystawia je pod
niezmiennymi adresami. Cel: **opublikowany feed zawsze obejmuje dzisiejszy dzień
i sięga możliwie daleko w przód.**

```
http://localhost:8090/gtfs/wroclaw-gtfs.zip
http://localhost:8090/gtfs/gzm-extend-gtfs.zip
http://localhost:8090/gtfs/czestochowa-gtfs.zip
http://localhost:8090/gtfs/gorzow-gtfs.zip
http://localhost:8090/gtfs/zielona-gora-gtfs.zip
http://localhost:8090/gtfs/rybnik-gtfs.zip
http://localhost:8090/gtfs/jaslo-gtfs.zip
http://localhost:8090/gtfs/slupsk-gtfs.zip
http://localhost:8090/gtfs/polregio-gtfs.zip
http://localhost:8090/gtfs/olkusz-gtfs.zip
http://localhost:8090/gtfs/mld-gtfs.zip
http://localhost:8090/gtfs/ska-gtfs.zip
http://localhost:8090/gtfs/zkkm-gtfs.zip
http://localhost:8090/gtfs/index.json
```

Port zmienisz przez `FEEDSODT_PORT` w pliku `.env` (domyślnie 8090, bo 8080 bywa zajęte).

## Uruchomienie

```bash
docker compose up -d --build
```

To wszystko. Pierwszy przebieg startuje od razu i bierze komplet źródeł, żeby
`up -d` dawało pliki bez czekania. Potem każde źródło wraca **co trzy dni,
w swoim własnym slocie** — jedna godzina, jedno źródło, nigdy dwa naraz:

```
dzień 1/3   01:00 wroclaw   02:00 gzm-extend   03:00 czestochowa   04:00 gorzow   05:00 zielona-gora
dzień 2/3   01:00 rybnik    02:00 jaslo        03:00 slupsk        04:00 polregio 05:00 olkusz
dzień 3/3   01:00 mld       02:00 ska          03:00 zkkm
```

Slot ustawia się per źródło polami `run_day` i `run_at` w `sources.toml`,
długość cyklu — `FEEDSODT_INTERVAL_DAYS`. Dzień cyklu liczy się od stałej daty,
a nie od startu kontenera, więc restart nie przesuwa kolejki, a przełom
miesiąca nie robi krótszego odstępu (pułapka wyrażeń `day=*/3`).

Pliki lądują w `./gtfs` na hoście. Aktualny rozkład pokazuje
`python -m feedsodt list`.

Każdy przebieg **faktycznie pobiera** wszystkie feedy i porównuje ich treść — bez
skrótów na metadanych listy plików. Część źródeł podmienia plik pod niezmiennym
adresem i nie podaje ani rozmiaru, ani daty (Gorzów), więc jakiekolwiek zaufanie
do listingu oznaczałoby wieczne „bez zmian".

Źródło, które zawiodło, jest ponawiane **po 15 min, potem po 30 i po godzinie**
— tylko ono, nie cała reszta. Po trzech nieudanych próbach czeka do swojego
następnego slotu.

```bash
docker compose logs -f fetcher                            # podgląd przebiegu
docker compose exec fetcher python -m feedsodt run-once   # wymuszenie przebiegu
docker compose exec fetcher python -m feedsodt run-once --only polregio
docker compose exec fetcher python -m feedsodt list       # co jest skonfigurowane
```

## Źródła

| miasto | resolver | polityka | uwagi |
|---|---|---|---|
| [Wrocław](https://open-data.cui.wroclaw.pl/hdb/ft/6/) | `html` | `today_forward` | 69 wersji w nieposortowanej tabeli, okresy się nakładają |
| [GZM](https://otwartedane.metropoliagzm.pl/dataset/rozklady-jazdy-i-lokalizacja-przystankow-gtfs-wersja-rozszerzona) | `ckan` | `today_forward` | 9 paczek o rozłącznych oknach, scalane w jeden feed |
| [Częstochowa](https://mpk.czest.pl/public/cloud/index.php/s/uutbhxukj2pYM6T) | `nextcloud` | `single` | katalog na publikację, w środku 5 formatów |
| [Gorzów Wlkp.](https://bip.mzk-gorzow.com.pl/gtfs.html) | `html` | `single` | serwer wrażliwy na częste odpytywanie |
| [Zielona Góra](https://www.mzk.zgora.pl/dla-deweloperow) | `html` | `single` | nazwa pliku to hash, rozpoznanie po podpisie linku |
| [Rybnik](https://km.rybnik.pl/435/pliki-do-pobrania.html) | `html` | `single` | |
| [Jasło](https://www.mksjaslo.com.pl/strefa-pasazera/gtfs) | `html` | `today_forward` | `feed_info.txt` bez dat — okno z `calendar.txt` |
| [Słupsk](https://zimslupsk.pl/otwarte-dane.html) | `html` | `today_forward` | pliki nazwane datą publikacji |
| POLREGIO (cała Polska) | `direct` | `single` | przebudowywany (`transform`) i kształtowany (`shape`) |
| [ZKGKM Olkusz](https://api.odt.org.pl/feed/9/google_transit.zip) | `direct` | `single` | 28 linii bez barw i bez geometrii — `transform = "odt"` + `shape` |
| [MLD (Koleje Małopolskie)](https://gtfs.kolejemalopolskie.com.pl/GTFS-ST/GTFS.zip) | `direct` | `single` | 70 linii dowozowych, barwy z tabeli organizatora |
| [SKA (Koleje Małopolskie)](https://www.kolejemalopolskie.com.pl/rozklady_jazdy/kml-ska-gtfs.zip) | `direct` | `single` | perony trzymane w `stop_headsign` — przebudowa `ska,odt` + `shape` |
| [ZKKM Chrzanów](https://zkkm.pl/files/pl/gtfs/google-transit-3.zip) | `direct` | `single` | 26 linii w jednej barwie organizatora |

## Przebudowa feedu (`transform`)

Źródło bywa poprawne składniowo i bezużyteczne w praktyce. Wtedy w `sources.toml`
dopisuje się `transform = "<nazwa>"`, a feed po pobraniu przechodzi przez funkcję
naprawczą z `feedsodt/transform/`. Bez tego pola plik jest publikowany bajt w bajt.

Są trzy: `odt` — lekka, sterowana konfiguracją, dla feedów którym brakuje tylko
barw i wystawcy; oraz `polregio` i `ska` — pełne przebudowy konkretnych,
konkretnie zepsutych źródeł.

Pole `transform` przyjmuje kilka nazw po przecinku i wtedy feed przechodzi
przez nie po kolei (`transform = "ska,odt"`), więc naprawa modelu przewoźnika
nie musi powtarzać wspólnego kolorowania.

### `odt` — barwy linii i wystawca

`route_color` przychodzi puste w komplecie: 28/28 tras w Olkuszu, 70/70 w MLD,
26/26 w ZKKM, 8/8 w SKA. Planer rysuje wtedy wszystko jedną barwą i pasażer nie
odróżnia linii od linii. `feed_info.txt` z kolei wskazuje firmę, która zrobiła
rozkład dla przewoźnika (kiedyPrzyjedzie.pl, R&G PLUS) — a zgłoszenia o błędach
w **naszej** paczce mają trafiać do nas.

```toml
transform   = "odt"
route_color = "0462A2"           # jedna barwa dla wszystkich linii
colors_file = "mld-routes.txt"   # albo barwa per linia, z config/colors/
```

`colors_file` to zwykły `routes.txt` — ten sam plik, który dostaje się od
organizatora, więc aktualizacja barw to podmiana pliku. Dopasowanie idzie po
`route_short_name`, bo `route_id` bywa wersjonowany i zmienia się przy każdym
wydaniu (MLD: `1359_A1` → `1360_A1`). Gdy podane są oba pola, tabela wygrywa,
a linie spoza niej dostają barwę domyślną. `route_text_color` liczy się
z jasności tła, więc dopisanie linii to jeden wpis, nie dwa.

Wystawcą opublikowanej paczki jest zawsze Otwarte Dane Transportowe
(`kontakt@odt.org.pl`) — przewoźnik zostaje nietknięty w `agency.txt`.
Dotyczy to również feedu POLREGIO.

### `ska` — peron ma być przystankiem, nie kierunkowskazem

Koleje Małopolskie trzymają numer peronu w `stop_headsign`:

```
trip_id,arrival_time,departure_time,stop_id,stop_sequence,stop_headsign,...
2025_2026_1151056,06:12:00,06:12:00,80416,0,"peron I, tor 9",...
```

`stop_headsign` to pole na kierunkowskaz widoczny przy konkretnym zatrzymaniu
(„dalej jako pociąg do Zakopanego"), nie na numer peronu. Skutki są trzy naraz:
planer wypisuje pasażerowi „peron I, tor 9" jako kierunek jazdy, wszystkie
perony stacji dzielą jeden punkt na mapie, a peronu nie da się znaleźć tam,
gdzie konsument go szuka — w `stops.txt`.

Przebudowa przenosi peron na jego miejsce. Stan pliku z 13.08.2026:

| w oryginale | po przebudowie |
|---|---|
| peron w `stop_headsign` w 26 234 / 26 242 wierszy | `platform_code` w `stops.txt`, `stop_headsign` usunięte |
| 149 przystanków = 149 stacji | **451**: 113 stacji nadrzędnych, 302 perony, 36 samodzielnych |
| wszystkie perony stacji w jednym punkcie | 295 peronów z własnymi współrzędnymi z listy PLK |
| `trip_headsign` puste w 1224 / 1224 | stacja docelowa kursu |
| `direction_id` puste w 1224 / 1224 | 0/1, zgodnie z kierunkiem jazdy |
| nazwy stacji wersalikami (`KRAKÓW GŁÓWNY`) | zapis z OSM (`Kraków Główny`) — 148 poprawionych |
| `BAŃSKA NIŻNA` na 0,0 | współrzędne z listy PLK |
| `shape_dist_traveled` przy pustym `shapes.txt` | usunięte — geometrię liczymy sami |

Trafienia peronów: 289 par peron+tor dokładnie, 6 przez środek peronu,
3 przy punkcie stacji, 40 to `peron BUS` (komunikacja zastępcza, peronu nie ma).
Nietrafione są dwa nowe perony Kraków Batowice, których nie ma jeszcze w OSM.

Rozbicie na perony ma jeszcze jeden skutek: kursy jadące tą samą trasą, ale
z innych peronów, mają teraz różne sekwencje przystanków, więc kształtów jest
241 zamiast 64 — i każdy prowadzi do właściwej krawędzi peronu.

### `polregio` — pełna przebudowa

**POLREGIO** — stan pliku źródłowego z 24.08.2026 i co z nim robimy:

| w oryginale | po przebudowie |
|---|---|
| 10620 tras — jedna na numer pociągu | **26 tras** — jedna na kategorię (`R`, `IR`, `BUS`, `K22`…) |
| `route_long_name` puste w 10620/10620 | rozwinięcie kategorii („REGIO", „InterREGIO") tam, gdzie jest pewne |
| `trip_headsign` puste w 39766/39766 | stacja docelowa kursu |
| brak `direction_id` | 0/1, zgodnie z kierunkiem jazdy |
| `K7 BUS` i `K22 BUS` z `route_type=2` | `3`, tak jak zwykłe `BUS` |
| `route_color` puste w 9412/10620 | barwa organizatora dla 19 kategorii, reszta firmowa czerwień `#DA2128` |
| perony ze współrzędnymi dworca (4387/4387) | **3753 własnych współrzędnych** z listy peronów PLK |
| `stops.txt` bez hierarchii | 1478 stacji nadrzędnych, perony przez `parent_station` |
| 1451 przystanków, przy których nic nie staje | usunięte |
| brak `feed_info.txt` | okres obowiązywania i wersja |

Relacja („Gdynia Główna — Hel") nie jest już osobną trasą, ale nie ginie:
kierunek jazdy siedzi w `direction_id`, a stacja docelowa w `trip_headsign`.
Grupowanie po relacji jest nadal liczone wewnątrz — bez niego nie da się
powiedzieć, który kurs jedzie „tam", a który „z powrotem".

Kolory ustawia tabela `CATEGORY_COLORS` w
[`transform/polregio.py`](feedsodt/transform/polregio.py). Kody organizatorów
wojewódzkich mają własne barwy — te, których pasażer szuka na mapie: `K7`
zielone, `K22` zielonożółte, `SKA1` błękitne, `SKA2` różowe. Kategoria spoza
tabeli dostaje firmową czerwień, więc dołożenie kolejnej to jeden wpis.
Kolor tekstu (czarny albo biały) dobiera się sam z jasności tła.

### Współrzędne peronów

Przewoźnicy podają jeden punkt na stację i przypisują go wszystkim peronom —
w POLREGIO peron I/1, peron II/2 i sam dworzec mają identyczne współrzędne we
wszystkich 4387 wierszach. Prawdziwe położenie bierzemy z listy peronów PLK
zbudowanej z OSM ([platforms-list.json](https://kasmar00.github.io/osm-plk-platform-validator/platforms-list.json)),
odświeżanej przy każdym przebiegu, z wersjonowaną kopią w `feedsodt/data/`
na wypadek braku sieci.

Trafienia na żywych danych (4245 używanych przystanków):

| | wierszy |
|---|---|
| trafiony peron **i tor** (`II/3` → peron 2, tor 3) | 3678 |
| trafiony sam peron — środek z jego torów | 75 |
| oznaczenie peronu bez trafienia | 13 |
| bez oznaczenia peronu (`BUS`, puste) — zostaje punkt stacji | 479 |

Dwie pułapki w tym pliku, obie obsłużone w [`platforms.py`](feedsodt/platforms.py):
klucze słownika gubią wielkie litery diakrytyczne (`Bardo Śląskie` →
`bardo-laskie`), więc dopasowanie idzie po `station_name`, a nie po kluczu;
numeracja peronów jest arabska, gdy POLREGIO podaje rzymską.

## Geometria przejazdu (`shape`)

Bez `shapes.txt` planer rysuje linię prostą między przystankami — przez pola
i przez wodę. Feed z `shape = "rail,bus"` w `sources.toml` przechodzi po
przebudowie przez routing po OSM: kolej i tramwaj po torach
(OpenRailRouting), autobusy po drogach (GraphHopper). Silniki stoją w tle
jako kontenery z profilu `shape` — patrz [`shaper/`](shaper/README.md).

```bash
mkdir -p osm
# -L jest konieczne: Geofabrik odbija 302 na nazwę z datą (poland-260823.osm.pbf).
# Bez niego zapisze się 242-bajtowa strona przekierowania, a silnik wywali się
# przy imporcie. --fail pilnuje, żeby błąd HTTP nie wylądował w pliku jako treść.
curl -L --fail -o osm/poland-latest.osm.pbf https://download.geofabrik.de/europe/poland-latest.osm.pbf
docker compose --profile shape up -d
```

Silniki są dostrojone do polskiej sieci — pociągi nie jeżdżą po torze
szerokim (LHS, 1520 mm), a autobusy zastępcze trzymają się dróg głównych
zamiast objazdów bocznymi. Co dokładnie i dlaczego: [`shaper/`](shaper/README.md).
**Zmiana modelu wymaga przeliczenia grafu** — GraphHopper odmawia wczytania
grafu zbudowanego z innym profilem, a OpenRailRouting po cichu wstaje ze starym.

Silniki, ich konfiguracja i sposób odpytywania pochodzą z
[gtfs-shaper-maker](https://github.com/jotazet/gtfs-shaper-maker) (MIT).
Własna jest orkiestracja w [`feedsodt/shaping/`](feedsodt/shaping/), bo trzy
rzeczy musiały być inaczej, żeby nie złamać gwarancji tego projektu:

* **Deterministyczne `shape_id`.** Oryginał nadaje je w kolejności kończenia
  wątków, więc ten sam feed dwa razy z rzędu ma inne sha256 — czyli republikacja
  przy każdym przebiegu i `200 OK` zamiast `304` u konsumentów. Tutaj `shape_id` to
  skrót sekwencji przystanków.
* **Cache geometrii.** Kluczem są współrzędne sekwencji, więc poprawka
  położenia peronu unieważnia dokładnie te kształty, które przez niego
  prowadzą. Pamiętane są też porażki (z ponowieniem po tygodniu) — 39 sekwencji
  POLREGIO wychodzi poza wycinek OSM, a każda próba to kaskada zapytań
  czekających na timeout.
* **Strumieniowy zapis.** Oryginał przepuszcza feed przez gtfs_kit i pandas,
  co przy 42 MB `stop_times.txt` gubi i pamięć, i stałe daty wpisów ZIP.

### Kiedy kształtu lepiej nie robić wcale

Router nigdy nie mówi „nie wiem". Poproszony o przystanek, którego nie ma
w grafie, **dosuwa go do najbliższego miejsca, które zna**, i zwraca linię
biegnącą zupełnie gdzie indziej. W danych nic tego nie odróżnia od wyniku
poprawnego, więc odsiewamy takie przypadki sami, dwoma niezależnymi sitami.

**Poza obszarem.** `shape_area = "poland"` w `sources.toml` włącza test
położenia przystanku w granicy administracyjnej z OSM
([`area.py`](feedsodt/area.py), granica wersjonowana w `feedsodt/data/`).
Kurs, który wyjeżdża poza obszar, nie jest nawet wysyłany do silnika — 910
kursów POLREGIO do Berlina, Cottbus, Bützow czy Görlitz zostaje bez
geometrii. Granica rozdziela pary odległe o 2 km: Zgorzelec od Görlitz,
Gubin od Guben, Słubice od Frankfurtu.

**Linia z dala od przystanków.** Gdy policzona geometria mija któryś
przystanek o więcej niż 500 m, kształt leci do kosza. Na zdrowych danych 99%
przystanków leży w granicach 103 m od linii (peron stoi obok osi toru),
a wynik zepsuty odstawał o 16–19 km — próg rozdziela te dwa światy z zapasem.
To sito wyłapało 29 kształtów, w tym wszystkie 23 z ostatniej deski ratunku
(`route_endpoints`, czyli odcinek od pierwszego do ostatniego przystanku
z pominięciem reszty).

Gdy silniki nie odpowiadają albo pokrycie spadnie poniżej 50%, źródło jest
oznaczone jako nieudane i **poprzednia wersja zostaje opublikowana** — feed
bez połowy geometrii jest gorszy niż wczorajszy komplet.

### Wyniki

POLREGIO, wycinek ogólnopolski:

| | |
|---|---|
| unikalnych sekwencji przystanków | 4420 (dla 38736 kursów w obszarze) |
| kształtów policzonych | 4391 — 4246 przez map matching, 145 przez routing |
| kursy z geometrią | 38386 / 38736 (**99,1%**) |
| odchylenie przystanku od linii | mediana 40 m, 99. centyl 103 m, maks. 394 m |
| pierwszy przebieg / kolejne | 2 min / 8 s (z cache'u) |
| bez geometrii z założenia | 910 kursów za granicę, 120 jednoprzystankowych |

Żeby pokryć kursy transgraniczne, trzeba skleić wycinki `poland`,
`brandenburg`, `mecklenburg-vorpommern` i `sachsen` (osmium-tool), przeliczyć
grafy i dołożyć granicę Niemiec obok polskiej.

Dlaczego to nie jest zwykłe „pobierz najnowszy plik":

* **Wrocław** trzyma 69 wersji w tabeli, która **nie jest posortowana po wersji**,
  a okresy obowiązywania się nakładają. Najnowsza opublikowana paczka potrafi
  zaczynać się dopiero jutro.
* **GZM** nie publikuje wersji jednego pliku, tylko tnie jeden feed na 9 paczek
  o rozłącznych oknach dat. Paczka o najnowszej dacie publikacji obejmuje
  najodleglejsze dni — sama w sobie nie zawiera **ani jednego kursu** przez
  najbliższe dwa tygodnie. Dopiero komplet, scalony w jeden plik, jest używalnym
  rozkładem.

## Zasoby

Zmierzone na komplecie 13 feedów, z ciepłym cache'em geometrii:

| | |
|---|---|
| przebieg wszystkich źródeł | **40 s**, szczyt RSS **125 MB** |
| najcięższe źródło (polregio) | 10 s, szczyt RSS 119 MB |
| pozostałe źródła | poniżej 2 s, 50–80 MB (z czego ~50 MB to sam Python) |
| silniki routujące (gotowy graf) | rail 53–70 MB, bus 835–875 MB |
| import grafu, jednorazowo | do 2,6 GB — patrz `shaper/README.md` |
| pierwsze liczenie geometrii polregio | ok. 3,5 min, jednorazowo |

W praktyce w slocie budzi się jedno źródło, więc typowa noc to kilkanaście
sekund pracy i ok. 60 MB — poza dniem, w którym wypada polregio.

Trzy rzeczy trzymają te liczby w ryzach i warto o nich wiedzieć przy zmianach:

* **Geometria nie leży w pamięci.** Kształty wracają z cache'a SQLite po jednym
  przy zapisie `shapes.txt`, w kolejności posortowanych `shape_id`. Trzymanie
  kompletu 943 tys. punktów kosztowało 156 MB.
* **Sekwencje przystanków to dwie równoległe listy**, a identyfikatory są
  sprowadzone do jednego obiektu na przystanek — czytnik CSV tworzy nowy napis
  dla każdego z 704 tys. wierszy, choć różnych przystanków jest 4245. Kosztowało
  to 99 MB.
* **`stop_times.txt` jest kopiowane strumieniowo** wszędzie, gdzie nie musimy go
  przepisywać. 42 MB POLREGIO nigdy nie trafia na stertę w całości.

Na słabszej maszynie warto zejść z `FEEDSODT_SHAPE_WORKERS` do 4 — geometria
liczy się dłużej, ale tylko przy pierwszym przebiegu.

## Polityki

**`single`** — źródło daje jeden plik, bierzemy go.

**`today_forward`** — źródło daje wiele paczek. Bierzemy tę obejmującą dzisiaj
i sięgającą najdalej, a za nią doklejamy kolejne, z których każda zaczyna się
**dokładnie** dzień po końcu poprzedniej. Warunek „dokładnie" jest istotny:
paczka zachodząca na już wybrany zakres zdublowałaby kursy w dniach wspólnych,
a zaczynająca się później zostawiłaby dziurę w rozkładzie.

W praktyce: u GZM łańcuch spina wszystkie 9 paczek w komplet 14 dni; u Wrocławia
kończy się na pierwszej, bo kolejne się z nią nakładają.

## Dodanie miasta

Zwykle bez pisania kodu — wystarczy blok w [`config/sources.toml`](config/sources.toml).
Plik jest montowany do kontenera, a konfiguracja wczytuje się na nowo przed każdym
przebiegiem, więc **restart nie jest potrzebny**. Żeby sprawdzić zmianę od razu:

```bash
docker compose exec fetcher python -m feedsodt list
docker compose exec fetcher python -m feedsodt run-once
```

Strona z odnośnikiem, gdzie w adresie jest słowo „gtfs":

```toml
[[source]]
name         = "nowe-miasto"
output       = "nowe-miasto-gtfs.zip"
resolver     = "html"
url          = "https://przyklad.pl/dane"
link_pattern = "gtfs.*\\.zip$"
select       = "single"
```

Gdy nazwa pliku nic nie mówi (Zielona Góra serwuje `dla_deweloperow_74ib2.zip`),
rozpoznajemy po podpisie odnośnika:

```toml
link_text = "GTFS"
```

Feed, który po pobraniu wymaga naprawy albo geometrii, i nie powinien blokować
sobą reszty:

```toml
transform   = "odt"          # albo "polregio"/"ska"; z feedsodt/transform/
route_color = "0462A2"       # dla "odt": barwa linii (albo colors_file)
shape       = "rail,bus"     # wymaga profilu "shape" w docker compose
shape_area  = "poland"       # kurs poza obszarem zostaje bez geometrii
run_day     = 1              # dzień w cyklu (0..FEEDSODT_INTERVAL_DAYS-1)
run_at      = "04:00"        # godzina; razem z run_day daje własny slot
```

Gdy wersji jest wiele, `hint_date` wyłuskuje datę początku obowiązywania z tekstu
wiersza — dzięki temu Wrocław pobiera 5 plików zamiast 69:

```toml
hint_date  = "(\\d{2})(\\d{2})(\\d{4})"
hint_order = "dmy"          # albo "ymd"
select     = "today_forward"
```

Portal na CKAN (większość polskich portali danych otwartych):

```toml
resolver = "ckan"
base_url = "https://przyklad.pl"
dataset  = "nazwa-zbioru"
```

Publiczny share Nextcloud:

```toml
resolver     = "nextcloud"
base_url     = "https://przyklad.pl/cloud"
token        = "TOKEN_Z_URL"
folder       = "Dane rozkładowe"
file_pattern = "^GTFS_.*\\.zip$"
```

Portal, który nie pasuje do żadnego z trzech, wymaga resolvera — jednej funkcji
w `feedsodt/sources/`, która zwraca listę `Candidate`. Resolver **nie pobiera
plików i nie decyduje, co jest najnowsze**; odpowiada tylko na pytanie „co tu
jest do wzięcia". Resztą — HTTP, walidacją, scalaniem, publikacją — zajmuje się
wspólny rdzeń.

## Konfiguracja

| zmienna | domyślnie | znaczenie |
|---|---|---|
| `FEEDSODT_INTERVAL_DAYS` | `3` | co ile dni wraca każde źródło |
| `FEEDSODT_RUN_AT` | `00:30` | godzina dla źródeł bez własnego `run_at` |
| `FEEDSODT_RETRY_MINUTES` | `15,30,60` | odstępy ponawiania po błędzie; puste = bez ponawiania |
| `FEEDSODT_RUN_ON_START` | `true` | przebieg zaraz po starcie kontenera |
| `FEEDSODT_OUTPUT_DIR` | `/data/gtfs` | katalog wyjściowy |
| `FEEDSODT_SOURCES` | `/app/config/sources.toml` | plik konfiguracji źródeł |
| `FEEDSODT_PORT` | `8090` | port nginx-a na hoście |
| `FEEDSODT_CACHE_DIR` | `<output>/.cache` | lista peronów i cache geometrii |
| `FEEDSODT_RAIL_URL` | `http://railrouting:8989` | silnik dla kolei i tramwaju |
| `FEEDSODT_BUS_URL` | `http://graphhopper-bus:8989` | silnik dla autobusów |
| `FEEDSODT_SHAPE_WORKERS` | `8` | równoległe zapytania do silników |
| `FEEDSODT_SHAPE_GPS_ACCURACY` | `50` | tolerancja map matchingu w metrach |
| `FEEDSODT_OSM_FILE` | `./osm/poland-latest.osm.pbf` | wycinek OSM dla silników (`.env`, tylko compose) |
| `TZ` | `Europe/Warsaw` | strefa czasowa harmonogramu |

## Gwarancje

* **Podmiana jest atomowa** (`os.replace`) — nginx nigdy nie odda połowy pliku.
* **Błąd nie kasuje danych.** Nieudane pobranie, uszkodzony ZIP, zmiana schematu
  u źródła — poprzednia wersja zostaje opublikowana, a błąd trafia do logów.
  Awaria jednego miasta nie wpływa na pozostałe.
* **Bez zmian = bez zapisu.** Feed jest pobierany codziennie, ale plik na dysku
  podmieniamy tylko wtedy, gdy zmieniła się jego treść (sha256). `mtime` pozostaje
  nietknięty, więc konsumenci dalej dostają `304 Not Modified`. Scalanie,
  przebudowa i kształtowanie są deterministyczne (stałe daty we wpisach ZIP,
  `shape_id` ze skrótu sekwencji), więc to samo wejście daje zawsze bajt w bajt
  ten sam plik.

  Zastrzeżenie: identyczność bajtów jest gwarantowana **w obrębie jednego
  środowiska**. Strumień deflate zależy od wersji zlib — kontener ma zlib 1.3.1,
  a host z nowszą dystrybucją może mieć zlib-ng, co przy identycznej treści
  tabel daje inne bajty. Uruchomienie `run-once` raz na hoście, raz w kontenerze
  powoduje więc jedną zbędną republikację. W normalnej pracy (zawsze kontener)
  problem nie występuje.
* **Awaria jednego źródła nie blokuje reszty i nie kończy sprawy.** Nieudane
  źródło wraca po 15, 30 i 60 minutach — samo, bez ruszania pozostałych.
* **Dwa przebiegi naraz są niemożliwe.** Blokada plikowa (`gtfs/.work/.lock`)
  obejmuje też osobne procesy, więc ręczne `run-once` w trakcie przebiegu
  dobowego odbije się z kodem 3 zamiast kasować demonowi katalogi robocze.

Źródła są przetwarzane **po kolei**, więc dzielą wyłącznie czas przebiegu — nic
poza tym. Osobny katalog roboczy, osobny plik wyjściowy (unikalność nazw jest
sprawdzana przy starcie), osobny wpis w `index.json`, własny `try/except`.
* **Publikowane jest tylko to, co przeszło walidację**: poprawny ZIP, komplet
  wymaganych tabel GTFS i dający się odczytać okres obowiązywania.
* **Przeterminowanie jest widoczne.** `index.json` podaje przy każdym feedzie
  `covers_today` i `days_ahead`, a log ostrzega, gdy feed nie obejmuje dzisiaj
  albo kończy się wcześniej, niż wypadnie następne sprawdzenie. Próg wynika
  z `FEEDSODT_INTERVAL_DAYS`, nie ze stałej — przy cyklu 3-dniowym feed
  kończący się za 3 dni jest już podejrzany.

## Wdrożenie na serwer

Po skopiowaniu katalogu wystarczy Docker z wtyczką `compose` — nic więcej się
nie instaluje.

**Czego nie kopiować:** `.venv/` (ścieżki są przywiązane do maszyny, na której
powstał) i `gtfs/.work/` (katalog roboczy, czyszczony przy każdym źródle).

**Co warto skopiować, choć nie trzeba:**

| katalog | waga | co daje |
|---|---|---|
| `osm/poland-latest.osm.pbf` | 2,0 GB | **wymagany** — kopiowanie tylko oszczędza pobieranie; instrukcja niżej |
| `shaper/cache/` | 641 MB | gotowe grafy routingu. **Na maszynie poniżej 8 GB RAM to nie jest opcja, tylko konieczność** — import zajmuje do 2,6 GB i wywróci mniejszy serwer. Praca na gotowym grafie to ok. 1 GB |
| `gtfs/.cache/` | 31 MB | policzona geometria i lista peronów; bez nich pierwsze kształtowanie POLREGIO trwa ok. 3,5 min |
| `gtfs/*.zip` | 116 MB | feedy są od razu na miejscu, zamiast czekać na pierwszy przebieg |

Sam kod z konfiguracją to 3 MB.

**Kolejność ma znaczenie.** Wycinek OSM musi być na miejscu **przed** pierwszym
`up`. Jeśli go nie ma, Docker utworzy w tym miejscu **katalog** (tak działają
bind mounty do nieistniejących ścieżek) i od tej pory `curl` będzie odbijał się
od „Is a directory", a silnik kolejowy — od „no OSM file mounted".

```bash
cp .env.example .env        # i przejrzyj: port, ścieżka do OSM, pamięć JVM

mkdir -p osm
# -L jest konieczne: Geofabrik odbija 302 na nazwę z datą (poland-260823.osm.pbf).
# Bez niego zapisze się 242-bajtowa strona przekierowania, a silnik wywali się
# przy imporcie. --fail pilnuje, żeby błąd HTTP nie wylądował w pliku jako treść.
curl -L --fail -o osm/poland-latest.osm.pbf https://download.geofabrik.de/europe/poland-latest.osm.pbf

# sprawdź, zanim ruszysz dalej — ma być ok. 2,0 GB i sygnatura OSMHeader
ls -lh osm/poland-latest.osm.pbf && head -c 16 osm/poland-latest.osm.pbf | xxd

docker compose --profile shape up -d --build
```

Gdyby Docker zdążył założyć katalog, sprzątnięcie wygląda tak (katalog należy
do roota, bo utworzył go demon):

```bash
docker compose --profile shape down
sudo rm -rf osm/poland-latest.osm.pbf
```

i wracasz do pobierania powyżej.

Obraz silnika kolejowego **powstaje lokalnie** — nie ma go w żadnym rejestrze,
bo jest budowany z OpenRailRouting mavenem. Pierwszy `up` na nowej maszynie musi
go zbudować (kilka minut, klonuje repozytorium i kompiluje). Stąd `--build`
w poleceniu wyżej i `pull_policy: build` w compose — bez tego `up` bez `--build`
próbowałby go ściągnąć i kończył na mylącym „pull access denied".

`--profile shape` jest obowiązkowe, jeśli którekolwiek źródło ma pole `shape` —
bez niego silniki nie wstaną i pięć feedów będzie zawodzić co cykl (poprzednie
wersje zostaną opublikowane, więc nic nie znika, ale nic się nie odświeża).

Sprawdzenie po starcie:

```bash
docker compose logs -f fetcher                   # przebieg startowy
docker compose exec fetcher python -m feedsodt list
curl -sI http://localhost:8090/gtfs/index.json
docker inspect -f '{{.State.Health.Status}}' feedsodt-fetcher
```

**Prawa do katalogu wyjściowego.** Kontener fetchera działa jako UID 1000
(świadomie — żeby pliki na hoście nie należały do roota). Jeśli użytkownik na
serwerze ma inny UID, `./gtfs` trzeba mu oddać:

```bash
sudo chown -R 1000:1000 gtfs
```

Inaczej pierwszy przebieg wywali się na zapisie i katalog zostanie pusty.

**Co jest wystawione na świat.** Tylko `web` na porcie `FEEDSODT_PORT` (domyślnie
8090) — tam leżą feedy i `index.json`. Silniki routujące są przypięte do pętli
zwrotnej (`127.0.0.1`), bo fetcher rozmawia z nimi po wewnętrznej sieci compose
i mapowanie na hosta służy wyłącznie diagnostyce. Nie rozszerzaj go bez potrzeby:
to nieuwierzytelnione usługi, a jedno `/route` przez pół Polski to realna praca
procesora dla każdego, kto je znajdzie.

Na serwerze publicznym postaw przed `web` reverse proxy z TLS — projekt nie
obsługuje HTTPS i nie powinien.

## Uruchomienie bez Dockera

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m feedsodt list
.venv/bin/python -m feedsodt run-once
```

## Testy

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Testy **nie ruszają sieci** — budują sztuczne paczki GTFS w katalogu tymczasowym
(`tests/conftest.py`) i sprawdzają logikę, w której realnie łatwo o subtelny błąd:

| plik | co pilnuje |
|---|---|
| `test_chain.py` | dobór paczek: układ GZM (9 rozłącznych), układ Wrocławia (nakładające się — nie wolno doklejać), dziury, kolejność wejścia, odsiewanie archiwum |
| `test_merge.py` | brak duplikatów `trip_id`, brak sierot, deduplikacja przystanków i kształtów, głosowanie większościowe, determinizm bajt w bajt, przerwanie przy zmianie schematu |
| `test_gtfs_window.py` | odczyt okna z `feed_info.txt` i ścieżka zapasowa przez `calendar.txt`, odrzucanie uszkodzonych archiwów |
| `test_sources.py` | rozpoznawanie odnośnika po adresie i po podpisie, podpowiedzi dat, walidacja `sources.toml`, sloty w cyklu wielodniowym i ich rotacja |
| `test_transform_polregio.py` | zwijanie tras do kategorii, `direction_id` i kierunkowskaz, współrzędne peronów, hierarchia stacja–peron, determinizm |
| `test_platforms.py` | translacja oznaczeń peronów, składanie diakrytyków w nazwach stacji, ścieżki zapasowe przy braku sieci |
| `test_shaping.py` | deterministyczne `shape_id`, cache (również porażek), próg pokrycia, odsiew kursów poza obszarem i kształtów odbiegających od przystanków |
| `test_area.py` | test położenia w granicy, dziury w wielokątach, pary stacji po dwóch stronach rzeki granicznej |
| `test_transform_odt.py` | barwy z tabeli i domyślne, dopasowanie po oznaczeniu linii, ODT jako wystawca, nienaruszalność pozostałych tabel, poprawność wersjonowanych tabel barw |
| `test_transform_ska.py` | przeniesienie peronu ze `stop_headsign` do `stops.txt`, hierarchia stacja–peron, naprawa współrzędnych 0,0, kierunkowskaz i kierunek jazdy |

Pełny przebieg po prawdziwych portalach to nadal `python -m feedsodt run-once`.
