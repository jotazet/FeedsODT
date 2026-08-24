"""Przebudowa feedu SKA (Koleje Małopolskie).

Feed jest składniowo poprawny i modelowo postawiony na głowie. Stan wejściowy
zmierzony na pliku z 13.08.2026 (8 tras, 1224 kursy, 149 przystanków,
26 242 wiersze ``stop_times``):

=================================  ==========================================
peron w ``stop_headsign``          ``"peron I, tor 9"`` w 26 234 / 26 242
przystanek = cała stacja           113 ze 149 stacji obsługuje >1 peron
``trip_headsign``                  puste w 1224 / 1224
``direction_id``                   puste w 1224 / 1224
nazwy stacji                       wersalikami (``KRAKÓW GŁÓWNY``)
``BAŃSKA NIŻNA``                   współrzędne 0,0 — zatoka Gwinejska
``shape_dist_traveled``            wypełnione, przy pustym ``shapes.txt``
=================================  ==========================================

Najgorsze jest pierwsze. ``stop_headsign`` to pole na kierunkowskaz widoczny
przy konkretnym zatrzymaniu („dalej jako pociąg do Zakopanego"), a nie na numer
peronu. Trzymanie tam peronu ma trzy skutki: planer wypisuje pasażerowi
„peron I, tor 9" jako kierunek jazdy, wszystkie perony stacji dzielą jeden
punkt na mapie, a informacji o peronie nie da się wyszukać tam, gdzie
konsument jej szuka — czyli w ``stops.txt``.

Przebudowa przenosi peron tam, gdzie jego miejsce: każda para (stacja, peron)
staje się osobnym przystankiem z ``platform_code``, własnymi współrzędnymi
z listy peronów PLK (:mod:`feedsodt.platforms`) i ``parent_station``
wskazującym stację. ``stop_times`` wskazuje peron, a ``stop_headsign`` jest
czyszczone.

Przy okazji z tego samego źródła bierzemy poprawną wielkość liter w nazwach
stacji i współrzędne dla ``BAŃSKA NIŻNA``.

Kolory tras i wystawcę robi osobno transformacja ``odt`` — w sources.toml stoi
``transform = "ska,odt"``.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import shutil
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..tables import CSV_DIALECT, read_table, write_table, zip_entry
from ..platforms import PlatformIndex, parse_platform_code
from . import Context, TransformError, transform

log = logging.getLogger(__name__)

#: Tabele przepisywane od nowa; reszta jest kopiowana bajt w bajt.
REBUILT = ("stops.txt", "stop_times.txt", "trips.txt")

#: ``peron I, tor 9`` -> ``I/9``; ``peron BUS`` -> ``BUS``; ``peron 5`` -> ``5``.
_HEADSIGN = re.compile(r"^peron\s+(\S+?)(?:\s*,\s*tor\s+(\S+))?$", re.IGNORECASE)

#: Współrzędne, którymi przewoźnik oznacza „nie wiem" — punkt w zatoce
#: Gwinejskiej. W feedzie KML SKA ma je stacja BAŃSKA NIŻNA, zapisane jako
#: samo „0", więc porównujemy liczbowo: „0", „0.0" i „0.000000" to to samo.
NULL_ISLAND = (0.0, 0.0)

STOP_COLUMNS = (
    "stop_id",
    "stop_code",
    "stop_name",
    "stop_lat",
    "stop_lon",
    "location_type",
    "parent_station",
    "platform_code",
)

_TRANSLIT = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")

@dataclass
class SkaStats:
    stations: int = 0
    platforms: int = 0
    stops_before: int = 0
    stops_after: int = 0
    stops_dropped: int = 0
    headsigns_filled: int = 0
    #: Skąd wzięły się współrzędne peronu.
    coords_track: int = 0
    coords_platform: int = 0
    coords_station: int = 0
    coords_no_code: int = 0
    renamed: int = 0
    fixed_null_island: list[str] = field(default_factory=list)
    unparsed: Counter = field(default_factory=Counter)


def platform_code(headsign: str) -> str:
    """``"peron I, tor 9"`` -> ``"I/9"``. Puste, gdy to nie jest opis peronu."""
    match = _HEADSIGN.match((headsign or "").strip())
    if not match:
        return ""
    platform, track = match.groups()
    return f"{platform}/{track}" if track else platform


@transform("ska")
def rebuild(source: Path, dest: Path, ctx: Context | None = None) -> SkaStats:
    """Przenosi perony ze ``stop_headsign`` do ``stops.txt`` i uzupełnia kursy."""
    ctx = ctx or Context()
    stats = SkaStats()

    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        missing = [
            name
            for name in ("routes.txt", "trips.txt", "stops.txt", "stop_times.txt")
            if name not in names
        ]
        if missing:
            raise TransformError(f"{source.name}: brakuje {', '.join(missing)}")

        stations = {row["stop_id"]: row for row in read_table(archive, "stops.txt")}
        stats.stops_before = len(stations)
        trips = read_table(archive, "trips.txt")
        times = read_table(archive, "stop_times.txt")
        if not times:
            raise TransformError(f"{source.name}: stop_times.txt jest puste")

        platforms = PlatformIndex.load(ctx.platforms_cache, ctx.session)
        stops, assignment = _build_stops(stations, times, platforms, stats)
        headsigns, directions = _describe_trips(times, assignment, stops, stats)

        window = _window(archive)
        stamp = (int(window[0][:4]), int(window[0][4:6]), int(window[0][6:8]), 0, 0, 0)

        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
            for name in sorted(names):
                if name.endswith("/") or name in REBUILT or not name.endswith(".txt"):
                    continue
                with archive.open(name) as src, out.open(zip_entry(name, stamp), "w") as target:
                    shutil.copyfileobj(src, target, 256 * 1024)

            write_table(out, "stops.txt", STOP_COLUMNS, stops, stamp)
            _write_stop_times(out, times, assignment, stamp)
            _write_trips(out, trips, headsigns, directions, stamp)

    stats.stops_after = len(stops)
    log.info(
        "ska: przystanki %d -> %d (%d nieużywanych usunięto, %d stacji nadrzędnych, "
        "%d peronów); współrzędne: %d z toru, %d ze środka peronu, %d z punktu stacji, "
        "%d bez oznaczenia",
        stats.stops_before,
        stats.stops_after,
        stats.stops_dropped,
        stats.stations,
        stats.platforms,
        stats.coords_track,
        stats.coords_platform,
        stats.coords_station,
        stats.coords_no_code,
    )
    log.info(
        "ska: uzupełniono %d kierunkowskazów i kierunek jazdy; poprawiono wielkość liter "
        "w %d nazwach stacji",
        stats.headsigns_filled,
        stats.renamed,
    )
    if stats.fixed_null_island:
        log.warning(
            "ska: %s miały w źródle współrzędne 0,0 — wzięte z listy peronów PLK",
            ", ".join(stats.fixed_null_island),
        )
    if stats.unparsed:
        log.warning(
            "ska: nierozpoznane opisy peronu: %s",
            ", ".join(f"{value!r}×{count}" for value, count in stats.unparsed.most_common(5)),
        )
    return stats


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text.translate(_TRANSLIT)).strip("_").upper()


def _build_stops(
    stations: dict[str, dict[str, str]],
    times: list[dict[str, str]],
    platforms: PlatformIndex,
    stats: SkaStats,
) -> tuple[list[dict[str, str]], dict[tuple[str, str], str]]:
    """Buduje stops.txt i mapę ``(stop_id, stop_headsign) -> nowy stop_id``.

    Stację nadrzędną zakładamy tylko tam, gdzie jest co grupować (≥2 perony).
    Przy jednym peronie sztuczny węzeł byłby czystym balastem, więc przystanek
    zostaje pod swoim dotychczasowym identyfikatorem.
    """
    used: dict[str, list[str]] = defaultdict(list)
    for row in times:
        code = platform_code(row.get("stop_headsign", ""))
        if not code and (row.get("stop_headsign") or "").strip():
            stats.unparsed[row["stop_headsign"]] += 1
        if code not in used[row["stop_id"]]:
            used[row["stop_id"]].append(code)

    taken = set(stations)
    output: list[dict[str, str]] = []
    assignment: dict[tuple[str, str], str] = {}

    # Przystanek, na którym nic się nie zatrzymuje, to pinezka na mapie
    # w miejscu, do którego nie jedzie żaden kurs.
    stats.stops_dropped = sum(1 for stop_id in stations if stop_id not in used)

    for stop_id in sorted(stations, key=lambda key: (_slug(stations[key]["stop_name"]), key)):
        if stop_id not in used:
            continue
        station = stations[stop_id]
        source_name = station["stop_name"]
        name = platforms.proper_name(source_name) or source_name
        if name != source_name:
            stats.renamed += 1

        lat, lon = station.get("stop_lat", ""), station.get("stop_lon", "")
        if not _has_coords(lat, lon):
            found = platforms.station(source_name)
            if found is None:
                raise TransformError(
                    f"przystanek {source_name!r} nie ma współrzędnych ani w feedzie, "
                    f"ani na liście peronów PLK"
                )
            lat, lon = f"{found.lat:.6f}", f"{found.lon:.6f}"
            stats.fixed_null_island.append(name)

        codes = sorted(used[stop_id])
        parent = ""
        if len(codes) > 1:
            parent = stop_id
            stats.stations += 1
            output.append(
                _row(stop_id, station, name, lat, lon, location_type="1", parent="", code="")
            )

        for code in codes:
            child_id = stop_id if not parent else _unique(f"{stop_id}-{_slug(code) or 'X'}", taken)
            located = platforms.locate(name, code) if code else None
            if located is not None:
                child_lat, child_lon = f"{located.lat:.6f}", f"{located.lon:.6f}"
                if located.quality == "track":
                    stats.coords_track += 1
                else:
                    stats.coords_platform += 1
            else:
                child_lat, child_lon = lat, lon
                # „peron BUS" i puste pole peronem nie są — nie ma ich czego
                # szukać na liście PLK, więc to nie jest brak trafienia.
                if parse_platform_code(code) is None:
                    stats.coords_no_code += 1
                else:
                    stats.coords_station += 1
            if parent:
                stats.platforms += 1
            output.append(
                _row(child_id, station, name, child_lat, child_lon, "0", parent, code)
            )
            assignment[(stop_id, code)] = child_id

    return output, assignment


def _row(
    stop_id: str,
    station: dict[str, str],
    name: str,
    lat: str,
    lon: str,
    location_type: str,
    parent: str,
    code: str,
) -> dict[str, str]:
    return {
        "stop_id": stop_id,
        "stop_code": station.get("stop_code", ""),
        "stop_name": name,
        "stop_lat": lat,
        "stop_lon": lon,
        "location_type": location_type,
        "parent_station": parent,
        "platform_code": code,
    }


def _has_coords(lat: str, lon: str) -> bool:
    """Czy przystanek ma użyteczne współrzędne.

    Porównanie liczbowe, nie tekstowe: brak współrzędnej bywa zapisany jako
    ``0``, ``0.0`` albo ``0.000000`` i wszystkie trzy znaczą to samo.
    """
    try:
        return (float(lat), float(lon)) != NULL_ISLAND
    except (TypeError, ValueError):
        return False


def _unique(candidate: str, used: set[str]) -> str:
    result = candidate
    suffix = 2
    while result in used:
        result = f"{candidate}-{suffix}"
        suffix += 1
    used.add(result)
    return result


def _describe_trips(
    times: list[dict[str, str]],
    assignment: dict[tuple[str, str], str],
    stops: list[dict[str, str]],
    stats: SkaStats,
) -> tuple[dict[str, str], dict[str, str]]:
    """Kierunkowskaz i kierunek jazdy, wyliczone z sekwencji przystanków.

    Oryginał ma oba pola puste we wszystkich 1224 kursach. Kierunkowskaz to
    stacja docelowa, a ``direction_id`` — jak w POLREGIO — bierze się z pary
    krańców: oba kierunki tej samej relacji dostają ten sam klucz, więc jeden
    z nich jest „tam", a drugi „z powrotem".
    """
    names = {row["stop_id"]: row["stop_name"] for row in stops}
    ordered: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in times:
        target = assignment.get((row["stop_id"], platform_code(row.get("stop_headsign", ""))))
        if target is None:
            continue
        try:
            ordered[row["trip_id"]].append((int(row["stop_sequence"]), target))
        except (KeyError, ValueError):
            continue

    ends: dict[str, tuple[str, str]] = {}
    for trip_id, entries in ordered.items():
        entries.sort()
        ends[trip_id] = (names[entries[0][1]], names[entries[-1][1]])

    headsigns = {trip_id: destination for trip_id, (_, destination) in ends.items()}
    stats.headsigns_filled = sum(1 for value in headsigns.values() if value)

    directions: dict[str, str] = {}
    for trip_id, (origin, destination) in ends.items():
        low, _ = sorted((origin, destination))
        directions[trip_id] = "0" if origin == low else "1"
    return headsigns, directions


def _window(archive: zipfile.ZipFile) -> tuple[str, str]:
    dates: list[str] = []
    for row in read_table(archive, "feed_info.txt"):
        dates += [row.get("feed_start_date", ""), row.get("feed_end_date", "")]
    for row in read_table(archive, "calendar.txt"):
        dates += [row.get("start_date", ""), row.get("end_date", "")]
    for row in read_table(archive, "calendar_dates.txt"):
        dates.append(row.get("date", ""))
    dates = [value.strip() for value in dates if len(value.strip()) == 8 and value.strip().isdigit()]
    if not dates:
        raise TransformError("nie da się ustalić okresu obowiązywania feedu")
    return min(dates), max(dates)


def _write_stop_times(
    out: zipfile.ZipFile,
    times: list[dict[str, str]],
    assignment: dict[tuple[str, str], str],
    stamp: tuple[int, ...],
) -> None:
    """Przepisuje stop_times na perony i czyści stop_headsign.

    ``shape_dist_traveled`` wypada: w oryginale jest wypełnione w kilometrach,
    a ``shapes.txt`` jest pusty, więc odległości nie mają do czego się odnosić.
    Geometrię dorabiamy potem sami i wtedy ta kolumna byłaby wprost sprzeczna
    z kształtem.
    """
    columns = [
        column
        for column in times[0].keys()
        if column not in ("shape_dist_traveled", "stop_headsign")
    ]
    with out.open(zip_entry("stop_times.txt", stamp), "w") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore", **CSV_DIALECT)
        writer.writeheader()
        for row in times:
            target = assignment.get((row["stop_id"], platform_code(row.get("stop_headsign", ""))))
            writer.writerow({**row, "stop_id": target or row["stop_id"]})
        stream.flush()
        stream.detach()


def _write_trips(
    out: zipfile.ZipFile,
    trips: list[dict[str, str]],
    headsigns: dict[str, str],
    directions: dict[str, str],
    stamp: tuple[int, ...],
) -> None:
    columns = list(trips[0].keys())
    for required in ("trip_headsign", "direction_id"):
        if required not in columns:
            columns.append(required)
    with out.open(zip_entry("trips.txt", stamp), "w") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore", **CSV_DIALECT)
        writer.writeheader()
        for trip in trips:
            trip_id = trip["trip_id"]
            writer.writerow(
                {
                    **trip,
                    "trip_headsign": headsigns.get(trip_id, trip.get("trip_headsign", "")),
                    "direction_id": directions.get(trip_id, trip.get("direction_id", "")),
                }
            )
        stream.flush()
        stream.detach()
