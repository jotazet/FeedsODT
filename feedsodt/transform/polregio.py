"""Przebudowa feedu GTFS POLREGIO.

Oryginał jest poprawny składniowo, ale w praktyce nie da się z niego zrobić
rozkładu dla pasażera. Stan wejściowy zmierzony na pliku z 24.08.2026
(10620 tras, 39766 kursów, 5696 przystanków, ważność 02.03–12.12.2026):

===============================  =========================
``route_long_name``              puste w 10620 / 10620
``route_desc``                   puste w 10620 / 10620
``route_text_color``             puste w 10620 / 10620
``route_color``                  puste w 9412 / 10620
``trip_headsign``                puste w 39766 / 39766
``trip_short_name``              wszystkie z ogonem spacji
``K7 BUS`` i ``K22 BUS``         ``route_type=2`` (kolej) zamiast 3
jedna trasa na numer pociągu     mediana 3 kursy na trasę
perony bez własnych współrzędnych  4387 / 4387
``stops.txt`` bez hierarchii     stacja i jej perony to niepowiązane punkty
nieużywane przystanki            1451 / 5696
===============================  =========================

Co z tym robimy:

**Trasy to kategorie.** W oryginale „trasa" to numer pociągu, więc planer
pokazuje 275 osobnych tras tam, gdzie pasażer widzi jedno połączenie
Gdynia — Hel. Zwijamy to do jednej trasy na kategorię (``R``, ``IR``, ``BUS``,
``K22``…) — ok. 26 wierszy zamiast 10620. Relacja nie ginie: kierunek jazdy
siedzi w ``direction_id``, a stacja docelowa w ``trip_headsign``.

Grupowanie po relacji jest nadal liczone, bo bez niego nie da się wyznaczyć
``direction_id``: opiera się ono na sprawdzonym fakcie, że **każda z 10620
tras oryginału ma dokładnie jedną relację** (zero tras o dwóch różnych parach
krańców), więc kierunek „od krańca A do krańca B" jest jednoznaczny.

**Perony dostają własne współrzędne** z listy peronów PLK (``..platforms``)
i zostają podpięte pod stacje przez ``parent_station``. Przystanki, do których
nie jedzie żaden kurs, wypadają.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import shutil
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .. import feedinfo
from ..colors import text_color_for
from ..tables import CSV_DIALECT, read_table, write_table, zip_entry
from ..platforms import PlatformIndex, parse_platform_code
from . import Context, TransformError, transform

log = logging.getLogger(__name__)

#: Firmowa czerwień POLREGIO, wzięta z ich logo (POLREGIO_logo-poziom.svg,
#: 7 wystąpień) i potwierdzona w arkuszu stylów polregio.pl (40 wystąpień).
#: Uwaga: krążące po sieci #DA291C to NIE jest ten kolor.
POLREGIO_RED = "DA2128"

#: Kolor per kategoria. Kategoria spoza tabeli dostaje firmową czerwień.
#: Kolor tekstu (czarny/biały) dobiera się sam na podstawie jasności tła,
#: więc dopisanie kategorii to jeden wpis, a nie dwa.
#:
#: Kody z prefiksem literowym to oznaczenia linii nadawane przez organizatorów
#: wojewódzkich — SKA* w Małopolsce, S* i K* na Śląsku i Mazowszu — i mają
#: własne barwy w rozkładach organizatora, których pasażer szuka na mapie.
CATEGORY_COLORS: dict[str, str] = {
    "SKA1": "29ACE0",
    "SKA2": "D23A7B",
    "SKA3": "F3B936",
    "K2": "9E8D81",
    "K22": "97BE2B",
    "K3": "E74F14",
    "S3": "E74F14",
    "S31": "5F307C",
    "K32": "73BAAA",
    "K33": "912281",
    "S34": "FAF40E",
    "K5": "EDA0C4",
    "K51": "643D18",
    "S51": "CB71F6",
    "K52": "B91E23",
    "K63": "4A66A4",
    "K7": "0A8E42",
    "K71": "2B2855",
    "M7": "B6CD8F",
}

#: Sufiks „P" w kodzie linii znaczy „przyspieszony" — to wariant tej samej
#: linii, nie osobna linia. Przewoźnik wystawia go jako oddzielną kategorię,
#: więc ten sam pociąg potrafi jechać najpierw jako K33P, potem jako K33:
#: planer pokazuje dwie linie w dwóch kolorach tam, gdzie pasażer widzi jedną.
#:
#: Wzorzec jest celowo wąski — pasuje tylko do kodów linii (``RP``, ``K33P``,
#: ``S31P``, ``M7P``), a nie do wszystkiego, co kończy się na P. Sprawdzone na
#: żywym feedzie: pięć takich kategorii (RP, K3P, K7P, K33P, K52P) i każda ma
#: w tym samym pliku odpowiednik bez P.
_ACCELERATED = re.compile(r"^(R|K\d+|S\d+|M\d+)P$")

#: Rozwinięcia skrótów, których jesteśmy pewni — trafiają do
#: ``route_long_name``. Kody typu K7, K33P czy RP to oznaczenia linii nadawane
#: przez organizatorów wojewódzkich; świadomie ich nie rozwijamy, bo zmyślona
#: nazwa jest gorsza niż jej brak, a ``route_short_name`` i tak niesie kod.
CATEGORY_NAMES = {
    "R": "REGIO",
    "Os": "pociąg osobowy",
    "IR": "InterREGIO",
    "BUS": "zastępcza komunikacja autobusowa",
    "SKA2": "Szybka Kolej Aglomeracyjna",
    "SKA3": "Szybka Kolej Aglomeracyjna",
}

#: Tabele przepisywane od nowa; reszta jest kopiowana bajt w bajt.
REBUILT = ("routes.txt", "trips.txt", "stops.txt", "feed_info.txt")

ROUTE_COLUMNS = (
    "route_id",
    "agency_id",
    "route_short_name",
    "route_long_name",
    "route_type",
    "route_color",
    "route_text_color",
)

TRIP_COLUMNS = (
    "route_id",
    "service_id",
    "trip_id",
    "trip_headsign",
    "trip_short_name",
    "direction_id",
    "block_id",
    "wheelchair_accessible",
    "bikes_allowed",
)

STOP_COLUMNS = (
    "stop_id",
    "stop_name",
    "stop_lat",
    "stop_lon",
    "location_type",
    "parent_station",
    "platform_code",
)

_TRANSLIT = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")

@dataclass
class RebuildStats:
    routes_before: int = 0
    routes_after: int = 0
    #: Trasy, którym sufiks „P" (przyspieszony) sprowadzono do wariantu zwykłego.
    accelerated_merged: int = 0
    relations: int = 0
    trips: int = 0
    headsigns_filled: int = 0
    route_type_fixed: int = 0
    skipped_trips: int = 0
    stops_before: int = 0
    stops_after: int = 0
    stops_dropped: int = 0
    stations: int = 0
    #: Skąd wzięły się współrzędne peronu: dokładny tor / środek peronu /
    #: punkt stacji (nie znaleziono) / brak oznaczenia peronu (np. „BUS").
    coords_track: int = 0
    coords_platform: int = 0
    coords_station: int = 0
    coords_no_code: int = 0


def normalize_category(category: str) -> str:
    """``K33P`` -> ``K33``, ``RP`` -> ``R``. Reszta bez zmian.

    Wariant przyspieszony to ta sama linia — ma dostać ten sam identyfikator
    trasy i tę samą barwę co wariant zwykły.
    """
    category = category.strip()
    return category[:-1] if _ACCELERATED.match(category) else category


def _slug(text: str) -> str:
    """Nazwa stacji -> fragment identyfikatora: GDYNIA_GLOWNA."""
    return re.sub(r"[^A-Za-z0-9]+", "_", text.translate(_TRANSLIT)).strip("_").upper()


def _scan_stop_times(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Jedno przejście przez stop_times.txt: pierwszy i ostatni przystanek
    każdego kursu oraz zbiór faktycznie odwiedzanych przystanków.

    Plik ma 42 MB, więc czytamy strumieniowo i trzymamy tylko to, co wyżej
    potrzebne — nie całe sekwencje.
    """
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    used: set[str] = set()
    with archive.open("stop_times.txt") as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        for row in reader:
            trip = row["trip_id"]
            stop = row["stop_id"]
            if trip not in first:
                first[trip] = stop
            last[trip] = stop
            used.add(stop)
    return first, last, used


def _route_type_for(category: str) -> str:
    """Komunikacja zastępcza to autobus, nawet gdy nazwa kategorii zaczyna się
    od kodu linii (K7 BUS). W oryginale te 20 tras miało typ kolejowy."""
    return "3" if "BUS" in category.upper() else "2"


@transform("polregio")
def rebuild(source: Path, dest: Path, ctx: Context | None = None) -> RebuildStats:
    """Przepisuje feed POLREGIO: zwija trasy do kategorii, uzupełnia kierunek
    i kierunkowskaz kursu, nadaje peronom prawdziwe współrzędne."""
    stats = RebuildStats()
    ctx = ctx or Context()

    with zipfile.ZipFile(source) as archive:
        missing = [
            name
            for name in ("routes.txt", "trips.txt", "stops.txt", "stop_times.txt")
            if name not in archive.namelist()
        ]
        if missing:
            raise TransformError(f"{source.name}: brakuje {', '.join(missing)}")

        stop_rows = read_table(archive, "stops.txt")
        stop_names = {row["stop_id"]: row["stop_name"] for row in stop_rows}
        categories = {
            row["route_id"]: normalize_category(row["route_short_name"])
            for row in read_table(archive, "routes.txt")
        }
        agencies = read_table(archive, "agency.txt")
        agency_id = agencies[0].get("agency_id", "PR") if agencies else "PR"
        trips = read_table(archive, "trips.txt")
        first, last, used = _scan_stop_times(archive)

        stats.routes_before = len(categories)
        stats.accelerated_merged = sum(
            1
            for row in read_table(archive, "routes.txt")
            if normalize_category(row["route_short_name"]) != row["route_short_name"].strip()
        )
        stats.trips = len(trips)
        stats.stops_before = len(stop_rows)

        routes, assignment = _build_routes(trips, categories, first, last, stop_names, stats)
        stats.routes_after = len(routes)

        platforms = PlatformIndex.load(ctx.platforms_cache, ctx.session)
        stops = _build_stops(stop_rows, used, platforms, stats)

        window = _window(
            read_table(archive, "calendar.txt"), read_table(archive, "calendar_dates.txt")
        )
        stamp = (int(window[0][:4]), int(window[0][4:6]), int(window[0][6:8]), 0, 0, 0)

        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
            for name in sorted(archive.namelist()):
                if name.endswith("/") or name in REBUILT:
                    continue
                # Kopia strumieniowa — stop_times.txt nie mieści się wygodnie w pamięci.
                with archive.open(name) as src, out.open(zip_entry(name, stamp), "w") as target:
                    shutil.copyfileobj(src, target, 256 * 1024)

            write_table(out, "stops.txt", STOP_COLUMNS, stops, stamp)
            _write_routes(out, routes, agency_id, stamp)
            _write_trips(out, trips, assignment, stamp, stats)
            _write_feed_info(out, window, len(routes), stamp)

    if stats.accelerated_merged:
        log.info(
            "polregio: %d tras z wariantem przyspieszonym (sufiks P) sprowadzono "
            "do kategorii podstawowej",
            stats.accelerated_merged,
        )
    log.info(
        "polregio: %d tras -> %d kategorii (%d relacji), %d kursów, "
        "uzupełniono %d kierunkowskazów, naprawiono typ %d tras autobusowych",
        stats.routes_before,
        stats.routes_after,
        stats.relations,
        stats.trips,
        stats.headsigns_filled,
        stats.route_type_fixed,
    )
    log.info(
        "polregio: przystanki %d -> %d (%d nieużywanych usunięto, %d stacji nadrzędnych); "
        "współrzędne peronów: %d z toru, %d ze środka peronu, %d bez trafienia, %d bez oznaczenia",
        stats.stops_before,
        stats.stops_after,
        stats.stops_dropped,
        stats.stations,
        stats.coords_track,
        stats.coords_platform,
        stats.coords_station,
        stats.coords_no_code,
    )
    if stats.skipped_trips:
        log.warning("polregio: %d kursów bez przystanków — pominięte", stats.skipped_trips)
    return stats


@dataclass(frozen=True)
class _Route:
    route_id: str
    short_name: str
    long_name: str
    route_type: str
    color: str


def _build_routes(
    trips: list[dict[str, str]],
    categories: dict[str, str],
    first: dict[str, str],
    last: dict[str, str],
    stop_names: dict[str, str],
    stats: RebuildStats,
) -> tuple[list[_Route], dict[str, tuple[str, str, str]]]:
    """Zwija trasy do kategorii i zwraca przypisanie
    ``trip_id -> (route_id, direction_id, headsign)``.

    Relacje (para krańców) nie trafiają już do ``routes.txt``, ale nadal są
    liczone — z nich bierze się ``direction_id``. Bez wspólnej dla obu
    kierunków pary krańców nie da się powiedzieć, który kurs jedzie „tam",
    a który „z powrotem".
    """
    relations: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    ends: dict[str, tuple[str, str]] = {}

    for trip in trips:
        trip_id = trip["trip_id"]
        if trip_id not in first:
            stats.skipped_trips += 1
            continue
        origin = stop_names.get(first[trip_id], first[trip_id])
        destination = stop_names.get(last[trip_id], last[trip_id])
        category = categories.get(trip["route_id"], "").strip()
        # Krańce sortujemy, żeby oba kierunki tej samej relacji dały ten sam klucz.
        low, high = sorted((origin, destination))
        relations[(category, low, high)].append(trip)
        ends[trip_id] = (origin, destination)

    stats.relations = len(relations)

    routes: list[_Route] = []
    route_ids: dict[str, str] = {}
    used_ids: set[str] = set()

    for category in sorted({key[0] for key in relations}):
        route_id = _unique_id(f"PR-{_slug(category) or 'X'}", used_ids)
        route_ids[category] = route_id
        color = CATEGORY_COLORS.get(category, POLREGIO_RED)
        routes.append(
            _Route(
                route_id=route_id,
                short_name=category,
                long_name=CATEGORY_NAMES.get(category, ""),
                route_type=_route_type_for(category),
                color=color,
            )
        )
        if "BUS" in category.upper() and category != "BUS":
            stats.route_type_fixed += 1

    assignment: dict[str, tuple[str, str, str]] = {}
    for (category, low, high), group in relations.items():
        route_id = route_ids[category]
        for trip in group:
            origin, destination = ends[trip["trip_id"]]
            direction = "0" if origin == low else "1"
            assignment[trip["trip_id"]] = (route_id, direction, destination)

    return routes, assignment


def _unique_id(candidate: str, used: set[str]) -> str:
    """Dwa różne oznaczenia mogą dać ten sam identyfikator, gdy różnią się
    tylko znakami, które slug wycina. Wtedy dokładamy licznik."""
    result = candidate
    suffix = 2
    while result in used:
        result = f"{candidate}-{suffix}"
        suffix += 1
    used.add(result)
    return result


def _build_stops(
    stop_rows: list[dict[str, str]],
    used: set[str],
    platforms: PlatformIndex,
    stats: RebuildStats,
) -> list[dict[str, str]]:
    """Buduje stops.txt: perony z własnymi współrzędnymi, podpięte pod stacje.

    Przystanki, do których nie jedzie żaden kurs, wypadają — w oryginale jest
    ich 1451 z 5696 i każdy jest osobną pinezką na mapie w miejscu, w którym
    nic się nie zatrzymuje.

    Stację nadrzędną zakładamy tylko tam, gdzie jest co grupować (≥2 perony).
    Dla 587 stacji o jednym przystanku sztuczny węzeł nadrzędny byłby czystym
    balastem.
    """
    stations: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in stop_rows:
        if row["stop_id"] in used:
            stations[row["stop_name"]].append(row)
        else:
            stats.stops_dropped += 1

    taken = {row["stop_id"] for row in stop_rows}
    output: list[dict[str, str]] = []

    for name in sorted(stations, key=lambda value: (_slug(value), value)):
        children = sorted(stations[name], key=lambda row: (row["platform_code"], row["stop_id"]))
        # Punkt stacji: najczęstsza współrzędna wśród jej przystanków. Prawie
        # zawsze wszystkie są identyczne — Counter obsługuje te 10 stacji,
        # w których źródło podaje dwie różne.
        anchor = Counter((row["stop_lat"], row["stop_lon"]) for row in children).most_common(1)
        station_lat, station_lon = anchor[0][0]

        parent = ""
        if len(children) > 1:
            parent = _unique_id(f"ST-{_slug(name) or 'X'}", taken)
            stats.stations += 1
            output.append(
                {
                    "stop_id": parent,
                    "stop_name": name,
                    "stop_lat": station_lat,
                    "stop_lon": station_lon,
                    "location_type": "1",
                    "parent_station": "",
                    "platform_code": "",
                }
            )

        for row in children:
            code = row["platform_code"]
            located = platforms.locate(name, code) if code else None
            if located is not None:
                lat, lon = f"{located.lat:.6f}", f"{located.lon:.6f}"
                if located.quality == "track":
                    stats.coords_track += 1
                else:
                    stats.coords_platform += 1
            else:
                lat, lon = station_lat, station_lon
                # „BUS" i puste pole to nie perony — nie ma ich czego szukać na
                # liście PLK, więc nie są brakiem trafienia, tylko brakiem pytania.
                if parse_platform_code(code) is None:
                    stats.coords_no_code += 1
                else:
                    stats.coords_station += 1
            output.append(
                {
                    "stop_id": row["stop_id"],
                    "stop_name": name,
                    "stop_lat": lat,
                    "stop_lon": lon,
                    "location_type": "0",
                    "parent_station": parent,
                    "platform_code": code,
                }
            )

    stats.stops_after = len(output)
    return output


def _write_routes(
    out: zipfile.ZipFile, routes: list[_Route], agency_id: str, stamp: tuple[int, ...]
) -> None:
    with out.open(zip_entry("routes.txt", stamp), "w") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        writer = csv.writer(stream, **CSV_DIALECT)
        writer.writerow(ROUTE_COLUMNS)
        for route in routes:
            writer.writerow(
                [
                    route.route_id,
                    agency_id,
                    route.short_name,
                    route.long_name,
                    route.route_type,
                    route.color,
                    text_color_for(route.color),
                ]
            )
        stream.flush()
        stream.detach()


def _write_trips(
    out: zipfile.ZipFile,
    trips: list[dict[str, str]],
    assignment: dict[str, tuple[str, str, str]],
    stamp: tuple[int, ...],
    stats: RebuildStats,
) -> None:
    with out.open(zip_entry("trips.txt", stamp), "w") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        writer = csv.writer(stream, **CSV_DIALECT)
        writer.writerow(TRIP_COLUMNS)
        for trip in trips:
            mapped = assignment.get(trip["trip_id"])
            if mapped is None:
                continue  # kurs bez przystanków — policzony w skipped_trips
            route_id, direction, headsign = mapped
            if headsign:
                stats.headsigns_filled += 1
            writer.writerow(
                [
                    route_id,
                    trip["service_id"],
                    trip["trip_id"],
                    headsign,
                    trip.get("trip_short_name", "").strip(),
                    direction,
                    trip.get("block_id", ""),
                    trip.get("wheelchair_accessible", ""),
                    trip.get("bikes_allowed", ""),
                ]
            )
        stream.flush()
        stream.detach()


def _window(calendar: list[dict[str, str]], exceptions: list[dict[str, str]]) -> tuple[str, str]:
    dates = [row["start_date"] for row in calendar] + [row["end_date"] for row in calendar]
    dates += [row["date"] for row in exceptions]
    dates = [value for value in dates if value and value.isdigit() and len(value) == 8]
    if not dates:
        raise TransformError("nie da się ustalić okresu obowiązywania feedu")
    return min(dates), max(dates)


def _write_feed_info(
    out: zipfile.ZipFile, window: tuple[str, str], route_count: int, stamp: tuple[int, ...]
) -> None:
    """Oryginał nie ma feed_info.txt — dokładamy go, bo bez niego okno
    obowiązywania trzeba zgadywać z kalendarza. Wystawcą jest ODT, nie
    przewoźnik: to my publikujemy tę paczkę pod stałym adresem."""
    feedinfo.write(
        out,
        zip_entry("feed_info.txt", stamp),
        window,
        f"feedsodt-polregio:{window[0]}-{window[1]}:{route_count}",
    )
