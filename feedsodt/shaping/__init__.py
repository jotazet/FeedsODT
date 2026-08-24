"""Dorabianie ``shapes.txt`` przez silniki routujące gtfs-shaper-maker.

Większość polskich feedów regionalnych nie ma geometrii przejazdu — planer
rysuje wtedy linię prostą między przystankami, przez pola i przez wodę.
Geometrię da się odtworzyć, przepuszczając sekwencję przystanków przez router
po danych OSM: dla kolei i tramwaju po torach (OpenRailRouting), dla autobusów
po drogach (GraphHopper).

Silniki stoją w tle jako osobne kontenery (patrz ``docker-compose.yml``,
profil ``shape``) i wystawiają zwykłe HTTP. Sam sposób odpytywania — kaskada
``/match`` → ``/route`` → odcinek krańcowy, upraszczanie RDP — jest wzięty
z gtfs-shaper-maker i leży w :mod:`.router`; ten moduł dokłada do niego to,
czego FeedsODT wymaga od każdego kroku publikującego plik:

**Wynik musi być deterministyczny.** Oryginalne ``main.py`` nadaje
``shape_id`` w kolejności, w jakiej wątki kończą pracę (``as_completed``), więc
ten sam feed dwa razy z rzędu daje inne identyfikatory i inne sha256. Przy
naszej regule „bez zmian = bez zapisu" oznaczałoby to codzienną republikację
i ciągłe `200 OK` zamiast `304` u konsumentów. Tutaj ``shape_id`` jest skrótem
sekwencji przystanków, a wiersze idą w kolejności posortowanej.

**Geometria jest cache'owana między przebiegami.** Feed POLREGIO obowiązuje
do grudnia i między dobami zmienia się w nim niewiele, a routowanie 4,5 tys.
sekwencji od zera to kilkadziesiąt minut. Klucz cache'a to współrzędne
sekwencji, więc poprawka współrzędnych peronu unieważnia dokładnie te kształty,
których dotyczy.

**Zapis jest strumieniowy.** ``main.py`` przepuszcza feed przez gtfs_kit
i pandas, co przy 42 MB ``stop_times.txt`` gubi zarówno pamięć, jak i stałe
daty wpisów ZIP, na których stoi determinizm.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import shutil
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from requests.adapters import HTTPAdapter

from ..area import Area
from ..tables import CSV_DIALECT, read_table, zip_entry
from .cache import ShapeCache
from .router import GraphHopperClient, fetch_trip_geometry

log = logging.getLogger(__name__)

#: Nazwa trybu w sources.toml -> (``route_type``, profil silnika, czy silnik
#: umie sam załatać dziury między odległymi przystankami).
#:
#: ``fill_gaps`` to rozszerzenie OpenRailRouting: przystanki kolejowe bywają
#: oddalone o kilkanaście kilometrów, więc czyste map matching zostawiłoby
#: między nimi linie proste. GraphHopper autobusowy tego nie ma i dla niego
#: kaskada zaczyna się od ``/route`` przez wszystkie przystanki.
MODES = {
    "tram": (0, "tram", True),
    "rail": (2, "rail", True),
    "bus": (3, "bus", False),
}

#: Kursy krótsze niż tyle przystanków zostają bez kształtu. Przy dwóch punktach
#: „kształt" to i tak sam odcinek między nimi, ale dla kolei bywa on realną
#: trasą (wahadła, kursy techniczne), więc granicę stawiamy nisko.
MIN_STOPS = 2

#: Przystanek „na Wyspie Zerowej" — zatoka Gwinejska, 0N 0E. Tak wygląda
#: brakująca współrzędna, której ktoś nie zostawił pustej: w feedzie KML SKA
#: ma ją stacja BAŃSKA NIŻNA na Podhalu. Router dostałby punkt oddalony
#: o 5,5 tys. km i zwrócił dla całego kursu geometrię bez związku z rzeczywistością.
NULL_ISLAND = (0.0, 0.0)

#: Poniżej tego pokrycia uznajemy przebieg za nieudany i zostawiamy poprzednią
#: opublikowaną wersję. Feed, w którym połowa kursów straciła geometrię, jest
#: gorszy niż wczorajszy komplet — a taki wynik znaczy zwykle, że silnik wstał
#: z niepełnym grafem albo wycinek OSM nie pokrywa obszaru feedu.
MIN_COVERAGE = 0.5

#: Powyżej tego pokrycia nie zawracamy głowy ostrzeżeniem.
GOOD_COVERAGE = 0.95

#: Jak daleko od policzonej linii wolno leżeć przystankowi, w metrach.
#:
#: Router nigdy nie mówi „nie wiem" — gdy nie potrafi dopasować sekwencji,
#: zwraca *jakąś* linię, bywa że kilkanaście kilometrów obok. Na zdrowych
#: danych POLREGIO 99% przystanków leży w granicach 41 m od linii (peron stoi
#: obok osi toru), a wynik zepsuty odstaje o kilometry — próg 500 m rozdziela
#: te dwa światy z dużym zapasem w obie strony.
MAX_STOP_DEVIATION_M = 500

SHAPE_COLUMNS = ("shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence")

#: Tyle miejsc po przecinku zapisujemy w shapes.txt — ok. 11 cm.
PRECISION = 6

#: Maksymalne odchylenie uproszczonej linii od oryginalnej geometrii, w metrach.
#: Wartość i algorytm za gtfs-shaper-maker: dość mało, żeby linia została na
#: torze i żeby każdy przystanek na niej pozostał; dość dużo, żeby wyrzucić
#: nadmiarowe punkty na prostych.
SIMPLIFY_TOLERANCE_M = 5.0


class ShapeError(Exception):
    """Kształtów nie da się dorobić — lepiej zostawić poprzednią wersję feedu."""


@dataclass(frozen=True)
class ShapeConfig:
    """Co i czym kształtujemy."""

    #: Podzbiór ``MODES``, wzięty z pola ``shape`` w sources.toml.
    modes: frozenset[str]
    rail_url: str
    bus_url: str
    workers: int = 8
    gps_accuracy: int = 50
    #: Obszar pokryty wycinkiem OSM. Kurs, który z niego wyjeżdża, nie dostaje
    #: kształtu — patrz :mod:`feedsodt.area`. ``None`` wyłącza sprawdzanie.
    area: Area | None = None
    #: Plik SQLite z geometrią z poprzednich przebiegów. ``None`` wyłącza cache.
    cache_path: Path | None = None

    def url_for(self, mode: str) -> str:
        return self.bus_url if mode == "bus" else self.rail_url


@dataclass
class ShapeStats:
    sequences: int = 0
    shaped: int = 0
    from_cache: int = 0
    #: Sekwencje pominięte na podstawie zapamiętanej wcześniej porażki.
    known_bad: int = 0
    #: Wszystkie kursy w feedzie — do raportu, nie do liczenia pokrycia.
    trips: int = 0
    #: Kursy, które w ogóle da się zakształtować: w wybranym trybie i o co
    #: najmniej ``MIN_STOPS`` przystankach. Kurs jednoprzystankowy nie dostanie
    #: geometrii nigdy, więc wliczanie go do pokrycia zamieniałoby wadę danych
    #: źródłowych w fałszywy alarm o niedziałającym silniku.
    shapeable: int = 0
    trips_shaped: int = 0
    #: Kursy pominięte, bo wyjeżdżają poza obszar pokryty wycinkiem OSM.
    trips_outside_area: int = 0
    #: Przystanki z brakującą współrzędną zapisaną jako 0,0.
    stops_without_coords: list[str] = field(default_factory=list)
    #: Kształty odrzucone, bo linia nie trzymała się przystanków.
    rejected: int = 0
    points: int = 0
    methods: dict[str, int] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        return self.trips_shaped / self.shapeable if self.shapeable else 0.0


@dataclass(frozen=True)
class _Sequence:
    """Jedna unikalna sekwencja przystanków — jeden kształt dla wszystkich
    kursów, które nią jadą."""

    mode: str
    shape_id: str
    points: tuple[tuple[float, float], ...]
    trip_ids: tuple[str, ...]


def parse_modes(value: str) -> frozenset[str]:
    """``"rail,bus"`` -> ``{"rail", "bus"}``. Pusta wartość = bez kształtowania."""
    modes = {part.strip().lower() for part in value.split(",") if part.strip()}
    unknown = modes - set(MODES)
    if unknown:
        raise ShapeError(
            f"nieznany tryb kształtowania: {', '.join(sorted(unknown))}; "
            f"dostępne: {', '.join(sorted(MODES))}"
        )
    return frozenset(modes)


def shape(source: Path, dest: Path, cfg: ShapeConfig) -> ShapeStats:
    """Przepisuje feed, dokładając ``shapes.txt`` i kolumnę ``shape_id``."""
    stats = ShapeStats()

    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        missing = [
            name
            for name in ("routes.txt", "trips.txt", "stops.txt", "stop_times.txt")
            if name not in names
        ]
        if missing:
            raise ShapeError(f"{source.name}: brakuje {', '.join(missing)}")

        trips = read_table(archive, "trips.txt")
        stats.trips = len(trips)
        coords = _stop_coords(archive, stats)
        modes_by_trip = _trip_modes(archive, trips, cfg.modes)
        sequences = _sequences(archive, modes_by_trip, coords, cfg.area, stats)
        stats.sequences = len(sequences)
        stats.shapeable = sum(len(sequence.trip_ids) for sequence in sequences)

        if not sequences:
            raise ShapeError(
                f"{source.name}: żaden kurs nie pasuje do trybów "
                f"{', '.join(sorted(cfg.modes))} — nie ma czego kształtować"
            )

        # Cache jest otwarty przez cały krok, bo służy nie tylko do omijania
        # pracy: geometria wraca z niego pojedynczo przy zapisie, zamiast
        # czekać w pamięci. Przy POLREGIO to różnica 156 MB.
        with ShapeCache.open(cfg.cache_path) as cache:
            keys = _fetch_all(sequences, cfg, stats, cache)

            shaped_trips: dict[str, str] = {}
            for sequence in sequences:
                if sequence.shape_id in keys:
                    for trip_id in sequence.trip_ids:
                        shaped_trips[trip_id] = sequence.shape_id
            stats.trips_shaped = len(shaped_trips)

            _guard_coverage(source, stats, cfg)

            stamp = _stamp(archive)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
                for name in sorted(names):
                    if name.endswith("/") or name in ("trips.txt", "shapes.txt"):
                        continue
                    with archive.open(name) as src, out.open(
                        zip_entry(name, stamp), "w"
                    ) as target:
                        shutil.copyfileobj(src, target, 256 * 1024)
                _write_shapes(out, keys, cache, stamp, stats)
                _write_trips(out, trips, shaped_trips, stamp)

    log.info(
        "shapes: %d kształtów dla %d sekwencji (%d z cache'a), %d punktów; "
        "kursy z geometrią: %d/%d (%.1f%%)",
        stats.shaped,
        stats.sequences,
        stats.from_cache,
        stats.points,
        stats.trips_shaped,
        stats.shapeable,
        100 * stats.coverage,
    )
    if stats.methods:
        log.info(
            "shapes: metody — %s",
            ", ".join(f"{name}={count}" for name, count in sorted(stats.methods.items())),
        )
    return stats


def _guard_coverage(source: Path, stats: ShapeStats, cfg: ShapeConfig) -> None:
    if stats.coverage < MIN_COVERAGE:
        raise ShapeError(
            f"{source.name}: geometrię dostało tylko {stats.trips_shaped} "
            f"z {stats.shapeable} kursów nadających się do kształtowania "
            f"({100 * stats.coverage:.1f}%) — silnik routujący albo wycinek OSM nie pokrywa "
            f"obszaru feedu; zostawiam poprzednią wersję"
        )
    if stats.coverage < GOOD_COVERAGE:
        log.warning(
            "shapes: %d z %d kursów nadających się do kształtowania zostało bez geometrii "
            "(%.1f%% pokrycia)",
            stats.shapeable - stats.trips_shaped,
            stats.shapeable,
            100 * stats.coverage,
        )
    if stats.stops_without_coords:
        log.warning(
            "shapes: %d przystanków bez współrzędnych (zapisane jako 0,0): %s — "
            "kursy przez nie przejeżdżające zostają bez geometrii",
            len(stats.stops_without_coords),
            ", ".join(sorted(stats.stops_without_coords)[:10]),
        )
    if stats.trips_outside_area:
        log.info(
            "shapes: %d kursów wyjeżdża poza obszar %s — zostają bez geometrii, "
            "bo router dosunąłby je do przypadkowego miejsca",
            stats.trips_outside_area,
            cfg.area.name if cfg.area else "objęty wycinkiem OSM",
        )
    if stats.rejected:
        log.info(
            "shapes: %d kształtów odrzuconych — linia odbiegała od przystanków dalej "
            "niż %d m",
            stats.rejected,
            MAX_STOP_DEVIATION_M,
        )
    degenerate = stats.trips - stats.shapeable - stats.trips_outside_area
    if degenerate:
        log.info(
            "shapes: %d kursów nie nadaje się do kształtowania (inny tryb albo mniej niż "
            "%d przystanki) — poza rachunkiem pokrycia",
            degenerate,
            MIN_STOPS,
        )


def _stop_coords(
    archive: zipfile.ZipFile, stats: ShapeStats | None = None
) -> dict[str, tuple[float, float]]:
    coords: dict[str, tuple[float, float]] = {}
    for row in read_table(archive, "stops.txt"):
        # Stacje nadrzędne (location_type=1) nie są odwiedzane przez kursy.
        if row.get("location_type", "0") not in ("", "0"):
            continue
        try:
            point = (float(row["stop_lat"]), float(row["stop_lon"]))
        except (KeyError, ValueError):
            continue
        if point == NULL_ISLAND:
            # Brak współrzędnej, a nie przystanek w zatoce Gwinejskiej.
            if stats is not None:
                stats.stops_without_coords.append(row.get("stop_name") or row["stop_id"])
            continue
        coords[row["stop_id"]] = point
    return coords


def _trip_modes(
    archive: zipfile.ZipFile, trips: list[dict[str, str]], modes: frozenset[str]
) -> dict[str, str]:
    """``trip_id -> tryb``, tylko dla kursów w wybranych trybach."""
    wanted = {MODES[mode][0]: mode for mode in modes}
    route_modes = {}
    for row in read_table(archive, "routes.txt"):
        try:
            route_type = int(row["route_type"])
        except (KeyError, ValueError):
            continue
        if route_type in wanted:
            route_modes[row["route_id"]] = wanted[route_type]
    return {
        trip["trip_id"]: route_modes[trip["route_id"]]
        for trip in trips
        if trip.get("route_id") in route_modes
    }


def _sequences(
    archive: zipfile.ZipFile,
    modes_by_trip: dict[str, str],
    coords: dict[str, tuple[float, float]],
    area: Area | None = None,
    stats: ShapeStats | None = None,
) -> list[_Sequence]:
    """Grupuje kursy po identycznej sekwencji przystanków.

    ``shape_id`` liczymy ze skrótu sekwencji, nie z licznika: dzięki temu
    dołożenie jednego kursu do feedu nie przenumerowuje wszystkich kształtów,
    a wynik nie zależy od kolejności, w jakiej skończyły wątki.
    """
    # Poza obszarem router nie odmawia — dosuwa przystanek do najbliższego
    # miejsca, które zna, i zwraca linię biegnącą zupełnie gdzie indziej.
    # Taki kurs ma zostać bez kształtu, a nie z kształtem zmyślonym.
    outside = (
        {stop_id for stop_id, (lat, lon) in coords.items() if not area.contains(lat, lon)}
        if area is not None
        else set()
    )

    # Dwie równoległe listy zamiast listy krotek, a identyfikatory przystanków
    # sprowadzone do jednego obiektu na przystanek: czytnik CSV tworzy nowy
    # obiekt tekstowy dla każdego z 704 tys. wierszy POLREGIO, choć różnych
    # przystanków jest 4245. Bez tego sama ta struktura zajmowała 99 MB.
    canonical = {stop_id: stop_id for stop_id in coords}
    ordered: dict[str, list[str]] = defaultdict(list)
    positions: dict[str, list[int]] = defaultdict(list)
    with archive.open("stop_times.txt") as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        for row in reader:
            trip_id = row["trip_id"]
            if trip_id not in modes_by_trip:
                continue
            try:
                order = int(row["stop_sequence"])
            except (KeyError, ValueError):
                continue
            stop_id = row["stop_id"]
            ordered[trip_id].append(canonical.get(stop_id, stop_id))
            positions[trip_id].append(order)

    groups: dict[tuple[str, tuple[str, ...]], list[str]] = defaultdict(list)
    for trip_id, entries in ordered.items():
        order = positions[trip_id]
        if any(later < earlier for earlier, later in zip(order, order[1:])):
            # Rzadka ścieżka: stop_times bywa zapisane nie po kolei.
            entries = [stop_id for _, stop_id in sorted(zip(order, entries))]
        stop_ids = tuple(entries)
        if any(stop_id in outside for stop_id in stop_ids):
            if stats is not None:
                stats.trips_outside_area += 1
            continue
        if len(stop_ids) < MIN_STOPS or any(stop_id not in coords for stop_id in stop_ids):
            continue
        groups[(modes_by_trip[trip_id], stop_ids)].append(trip_id)

    sequences = []
    for (mode, stop_ids), trip_ids in groups.items():
        sequences.append(
            _Sequence(
                mode=mode,
                shape_id=_shape_id(mode, stop_ids),
                points=tuple(coords[stop_id] for stop_id in stop_ids),
                trip_ids=tuple(sorted(trip_ids)),
            )
        )
    sequences.sort(key=lambda sequence: sequence.shape_id)
    return sequences


def _shape_id(mode: str, stop_ids: tuple[str, ...]) -> str:
    digest = hashlib.sha1(f"{mode}\n".encode() + "\n".join(stop_ids).encode()).hexdigest()
    return f"S{digest[:12]}"


def _cache_key(profile: str, gps_accuracy: int, points: tuple[tuple[float, float], ...]) -> str:
    """Klucz cache'a to *współrzędne*, nie identyfikatory przystanków — poprawka
    współrzędnych peronu ma unieważnić kształt, a zmiana samego stop_id nie."""
    payload = json.dumps(
        [profile, gps_accuracy, [[round(lat, PRECISION), round(lon, PRECISION)] for lat, lon in points]],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _fetch_all(
    sequences: list[_Sequence], cfg: ShapeConfig, stats: ShapeStats, cache: ShapeCache
) -> dict[str, str]:
    """Dopilnowuje, żeby geometria każdej sekwencji była w cache'u, i zwraca
    mapę ``shape_id -> klucz cache'a``.

    Zwracamy klucze, nie punkty: zapis czyta potem kształty pojedynczo, więc
    w pamięci nigdy nie leży komplet 943 tys. punktów.
    """
    clients = {}
    for mode in sorted(cfg.modes):
        _, profile, fill_gaps = MODES[mode]
        client = GraphHopperClient(
            cfg.url_for(mode), profile, gps_accuracy=cfg.gps_accuracy, fill_gaps=fill_gaps
        )
        # requests trzyma domyślnie 10 połączeń na hosta, a wątków puszczamy
        # więcej — bez tego każdy nadmiarowy wątek dostaje świeże TCP,
        # a urllib3 zasypuje log ostrzeżeniami "Connection pool is full".
        adapter = HTTPAdapter(pool_connections=len(cfg.modes), pool_maxsize=cfg.workers)
        client.session.mount("http://", adapter)
        client.session.mount("https://", adapter)
        clients[mode] = client

    _check_engines(clients, sequences)

    keys: dict[str, str] = {}
    pending: list[tuple[_Sequence, str]] = []

    for sequence in sequences:
        profile = MODES[sequence.mode][1]
        key = _cache_key(profile, cfg.gps_accuracy, sequence.points)
        found = cache.lookup(key)
        if found.hit:
            keys[sequence.shape_id] = key
            stats.from_cache += 1
            _count(stats, "cache")
        elif found.known_bad:
            # Wiemy, że tej sekwencji nie da się zroutować, i jest za wcześnie
            # na kolejną próbę — patrz RETRY_AFTER_DAYS w .cache.
            stats.known_bad += 1
            _count(stats, "skipped-cached")
        else:
            pending.append((sequence, key))

    if pending:
        log.info(
            "shapes: %d sekwencji z cache'a, %d znanych nieroutowalnych, "
            "%d do policzenia (%d wątków)",
            stats.from_cache,
            stats.known_bad,
            len(pending),
            cfg.workers,
        )

        def route(item: tuple[_Sequence, str]):
            sequence, key = item
            coords, method = fetch_trip_geometry(clients[sequence.mode], list(sequence.points))
            return sequence, key, coords, method

        with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
            for sequence, key, coords, method in pool.map(route, pending):
                if not coords:
                    _count(stats, method)
                    cache.put_failure(key, method)
                    continue
                simplified = _simplify(coords)
                strayed = _max_deviation(sequence.points, simplified)
                if strayed > MAX_STOP_DEVIATION_M:
                    # Router zwrócił linię, ale nie tę — patrz MAX_STOP_DEVIATION_M.
                    log.debug(
                        "shapes: odrzucam %s (%s) — przystanek %.0f m od linii",
                        sequence.shape_id,
                        method,
                        strayed,
                    )
                    stats.rejected += 1
                    _count(stats, f"{method}-odrzucony")
                    cache.put_failure(key, f"{method}-odrzucony")
                    continue
                _count(stats, method)
                cache.put(key, simplified, method)
                keys[sequence.shape_id] = key
    cache.commit()

    stats.shaped = len(keys)
    return keys


def _max_deviation(
    points: tuple[tuple[float, float], ...], line: list[list[float]]
) -> float:
    """Największa odległość przystanku od policzonej linii, w metrach.

    Mierzymy do *odcinków*, nie do wierzchołków: po uproszczeniu RDP prosty
    fragment toru ma tylko dwa punkty, więc przystanek w jego połowie bywa
    kilometry od najbliższego wierzchołka, leżąc dokładnie na linii.
    """
    if len(line) < 2:
        return float("inf")
    worst = 0.0
    for lat, lon in points:
        kx = math.cos(math.radians(lat)) * 111320.0
        ky = 110540.0
        px, py = lon * kx, lat * ky
        best = float("inf")
        for (start_lon, start_lat), (end_lon, end_lat) in zip(line, line[1:]):
            ax, ay = start_lon * kx, start_lat * ky
            bx, by = end_lon * kx, end_lat * ky
            dx, dy = bx - ax, by - ay
            length = dx * dx + dy * dy
            if length == 0:
                distance = math.hypot(px - ax, py - ay)
            else:
                along = ((px - ax) * dx + (py - ay) * dy) / length
                along = 0.0 if along < 0 else 1.0 if along > 1 else along
                distance = math.hypot(px - (ax + along * dx), py - (ay + along * dy))
            if distance < best:
                best = distance
                if best <= MAX_STOP_DEVIATION_M / 10:
                    break  # dość blisko, dalsze odcinki nic nie zmienią
        if best > worst:
            worst = best
    return worst


def _count(stats: ShapeStats, name: str) -> None:
    stats.methods[name] = stats.methods.get(name, 0) + 1


def _check_engines(clients: dict[str, GraphHopperClient], sequences: list[_Sequence]) -> None:
    """Jedno zapytanie na silnik, zanim puścimy tysiące. Bez tego niedziałający
    silnik objawiłby się dopiero jako „0% pokrycia" po kwadransie prób."""
    needed = {sequence.mode for sequence in sequences}
    for mode in sorted(needed):
        client = clients[mode]
        try:
            response = client.session.get(f"{client.base_url}/info", timeout=10)
            response.raise_for_status()
        except Exception as exc:
            raise ShapeError(
                f"silnik routujący dla trybu {mode!r} nie odpowiada pod {client.base_url} "
                f"({exc}); uruchom go: docker compose --profile shape up -d"
            ) from None


def _simplify(coords: list[list[float]], tolerance_m: float = SIMPLIFY_TOLERANCE_M):
    """Ramer-Douglas-Peucker — ta sama implementacja co w gtfs-shaper-maker,
    tylko wyciągnięta tutaj, bo tam siedzi w ``main.py`` obok orkiestracji.

    W odróżnieniu od przerzedzania „co n-ty punkt" trzyma linię w granicach
    ``tolerance_m`` od każdego punktu oryginału, więc łuki, rozjazdy i pozycje
    przystanków zostają na miejscu, a znikają tylko punkty na prostych.
    """
    if len(coords) <= 2:
        return coords

    lat0 = coords[len(coords) // 2][1]
    kx = math.cos(math.radians(lat0)) * 111320.0
    ky = 110540.0
    pts = [(lon * kx, lat * ky) for lon, lat in coords]

    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]
    while stack:
        start, end = stack.pop()
        ax, ay = pts[start]
        bx, by = pts[end]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        dmax, idx = 0.0, -1
        for index in range(start + 1, end):
            px, py = pts[index]
            if seg2 == 0:
                distance = math.hypot(px - ax, py - ay)
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / seg2
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                distance = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if distance > dmax:
                dmax, idx = distance, index
        if dmax > tolerance_m and idx != -1:
            keep[idx] = True
            stack.append((start, idx))
            stack.append((idx, end))
    return [coords[index] for index in range(len(coords)) if keep[index]]


def _stamp(archive: zipfile.ZipFile) -> tuple[int, int, int, int, int, int]:
    """Data wpisów ZIP przepisana z wejścia — krok kształtujący nie ma powodu
    jej zmieniać, a stałość jest tym, co pozwala porównywać sha256."""
    for info in archive.infolist():
        if not info.filename.endswith("/"):
            return info.date_time
    return (1980, 1, 1, 0, 0, 0)


def _write_shapes(
    out: zipfile.ZipFile,
    keys: dict[str, str],
    cache: ShapeCache,
    stamp: tuple[int, ...],
    stats: ShapeStats,
) -> None:
    """Zapisuje shapes.txt, czytając geometrię z cache'a po jednym kształcie.

    Kolejność bierze się z posortowanych ``shape_id``, więc wynik jest
    deterministyczny mimo że punkty nigdy nie leżą razem w pamięci.
    """
    with out.open(zip_entry("shapes.txt", stamp), "w") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        writer = csv.writer(stream, **CSV_DIALECT)
        writer.writerow(SHAPE_COLUMNS)
        for shape_id in sorted(keys):
            coords = cache.lookup(keys[shape_id]).coords or []
            for index, (lon, lat) in enumerate(coords):
                writer.writerow([shape_id, f"{lat:.6f}", f"{lon:.6f}", index])
                stats.points += 1
        stream.flush()
        stream.detach()


def _write_trips(
    out: zipfile.ZipFile,
    trips: list[dict[str, str]],
    shaped: dict[str, str],
    stamp: tuple[int, ...],
) -> None:
    columns = list(trips[0].keys()) if trips else ["trip_id"]
    if "shape_id" not in columns:
        columns.append("shape_id")
    with out.open(zip_entry("trips.txt", stamp), "w") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore", **CSV_DIALECT)
        writer.writeheader()
        for trip in trips:
            writer.writerow({**trip, "shape_id": shaped.get(trip["trip_id"], "")})
        stream.flush()
        stream.detach()
