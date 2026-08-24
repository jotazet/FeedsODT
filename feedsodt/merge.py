"""Scalanie kilku paczek GTFS o rozłącznych oknach dat w jeden feed.

Po co: GZM nie publikuje jednego pliku w wielu wersjach, tylko tnie jeden feed na
kilka paczek — najbliższe dni pojedynczo, dalszą przyszłość w blokach. Paczka
o najnowszej dacie publikacji obejmuje najodleglejsze dni, więc „weź najnowszy plik"
daje feed bez ani jednego kursu na najbliższe dwa tygodnie. Dopiero komplet paczek
to kompletny rozkład.

Na czym polega trudność: paczki nie są rozłączne w przestrzeni identyfikatorów.
Sprawdzone na żywych danych GZM (9 paczek, 21.08–10.09.2026):

* ``service_id`` „8" występuje w kilku paczkach i w każdej znaczy inne daty,
* ``trip_id`` powtarza się między sąsiednimi oknami (18529 wspólnych na 21416),
* ``stops`` — 7573 wspólnych rekordów, **zero** różnic w atrybutach,
* ``shape_id`` — 2176 wspólnych, **zero** różnic w geometrii,
* ``routes`` — 9 różnic na 460 (same opisy),
* ``agency`` — spójne.

Stąd strategia: prefiks paczki dostają wyłącznie ``service_id`` i ``trip_id``
(bo tylko one kolidują znaczeniowo), a tabele statyczne są deduplikowane.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .config import FeedWindow, Package
from .tables import CSV_DIALECT, zip_entry

log = logging.getLogger(__name__)

#: Kolumny, których wartości muszą zostać rozdzielone między paczkami.
PREFIXED_COLUMNS = ("trip_id", "service_id")

#: Dedup po kluczu głównym: wygrywa wersja najczęstsza wśród paczek.
#:
#: Nie „pierwsza", bo pierwsza paczka bywa anomalią. Przykład z żywych danych:
#: okno 20260821 to pojedynczy piątek, w którym linia 5011 kursowała skróconym
#: wariantem (159 kursów, kraniec „Chorzów Teatr Rozrywki"). W ośmiu pozostałych
#: oknach ta sama linia jedzie do Chebzia. Pierwszeństwo dawało nazwę
#: jednodniowego objazdu całemu 21-dniowemu feedowi; głosowanie daje 8:1
#: na wariant normalny.
PK_DEDUP = {
    "agency.txt": "agency_id",
    "stops.txt": "stop_id",
    "routes.txt": "route_id",
    "fare_attributes.txt": "fare_id",
}

#: Dedup całych grup wierszy — jeden shape_id to wiele punktów.
GROUP_DEDUP = {"shapes.txt": "shape_id"}

#: Generowany od nowa, nie kopiowany.
GENERATED = ("feed_info.txt",)

#: Nagłówek feed_info.txt, gdy żadna paczka go nie ma.
DEFAULT_FEED_INFO = [
    "feed_publisher_name",
    "feed_publisher_url",
    "feed_lang",
    "feed_start_date",
    "feed_end_date",
    "feed_version",
]

#: schedule_ZTM_2026.08.21_10917_0417.zip -> 10917
_PACKAGE_SEQ_RE = re.compile(r"_(\d{3,7})_\d{3,6}(?:\.[A-Za-z_]+)?\.zip$")


class MergeError(Exception):
    """Paczek nie da się bezpiecznie scalić — lepiej zostawić poprzedni plik."""


@dataclass
class MergeStats:
    rows: Counter = field(default_factory=Counter)
    conflicts: Counter = field(default_factory=Counter)
    #: Które kolumny się różniły — bez tego "215 konfliktów" nic nie mówi
    #: o tym, czy zginął opis trasy, czy jej typ.
    conflict_fields: dict[str, Counter] = field(default_factory=dict)
    skipped: Counter = field(default_factory=Counter)


def _label_for(package: Package, index: int) -> str:
    """Numer kolejny paczki z nazwy pliku — czytelny w trip_id i w feed_version."""
    match = _PACKAGE_SEQ_RE.search(package.filename)
    if match:
        return match.group(1)
    return f"w{index}"


def _labels(packages: list[Package]) -> list[str]:
    labels = [_label_for(package, index) for index, package in enumerate(packages)]
    # Prefiksy muszą być unikalne, inaczej rozdzielenie identyfikatorów nic nie daje.
    if len(set(labels)) != len(labels):
        log.warning("numery paczek nie są unikalne, używam prefiksów pozycyjnych")
        return [f"w{index}" for index in range(len(packages))]
    return labels


def _read_header(archive: zipfile.ZipFile, name: str) -> list[str] | None:
    with archive.open(name) as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        for row in csv.reader(stream):
            return row
    return None


def _iter_rows(archive: zipfile.ZipFile, name: str):
    """Strumieniowo, wiersz po wierszu — stop_times.txt to setki MB tekstu."""
    with archive.open(name) as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        reader = csv.reader(stream)
        next(reader, None)  # nagłówek zweryfikowany osobno
        yield from reader


def _collect_headers(
    archives: list[zipfile.ZipFile], packages: list[Package]
) -> dict[str, list[str]]:
    """Zbiera nagłówki i twardo pilnuje ich zgodności między paczkami.

    Cicha zmiana schematu u źródła jest gorsza niż nieudany przebieg: scalony plik
    wyglądałby poprawnie, a miałby poprzesuwane kolumny.
    """
    headers: dict[str, list[str]] = {}
    origin: dict[str, str] = {}

    for archive, package in zip(archives, packages):
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            header = _read_header(archive, name)
            if header is None:
                continue
            known = headers.get(name)
            if known is None:
                headers[name] = header
                origin[name] = package.filename
            elif known != header:
                raise MergeError(
                    f"{name}: różne nagłówki w paczkach {origin[name]} i {package.filename}\n"
                    f"  {origin[name]}: {','.join(known)}\n"
                    f"  {package.filename}: {','.join(header)}"
                )
    return headers


def _mode_for(name: str, header: list[str]) -> tuple[str, str | None]:
    """Zwraca (tryb, kolumna). Tryb wynika z zawartości nagłówka, więc nowe tabele
    rozszerzeń GZM obsłużą się same."""
    if name in GROUP_DEDUP and GROUP_DEDUP[name] in header:
        return "group", GROUP_DEDUP[name]
    if name in PK_DEDUP and PK_DEDUP[name] in header:
        return "pk", PK_DEDUP[name]
    if any(column in header for column in PREFIXED_COLUMNS):
        # Po prefiksowaniu identyfikatory są unikalne z definicji — nie ma czego deduplikować.
        return "prefix", None
    return "row", None


def _write_feed_info(
    out: zipfile.ZipFile,
    archives: list[zipfile.ZipFile],
    header: list[str],
    window: FeedWindow,
    labels: list[str],
    stamp: tuple[int, int, int, int, int, int],
) -> None:
    template: dict[str, str] = {}
    for archive in archives:
        if "feed_info.txt" not in archive.namelist():
            continue
        raw = archive.read("feed_info.txt").decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(raw)))
        if rows:
            template = dict(rows[0])
            break

    template["feed_start_date"] = f"{window.start:%Y%m%d}"
    template["feed_end_date"] = f"{window.end:%Y%m%d}"
    template["feed_version"] = "merged:" + "+".join(labels)

    # Bez tych kolumn nadpisane wartości zniknęłyby przy zapisie wg nagłówka.
    columns = list(header)
    for required in ("feed_start_date", "feed_end_date", "feed_version"):
        if required not in columns:
            columns.append(required)

    with out.open(zip_entry("feed_info.txt", stamp), "w") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        writer = csv.writer(stream, **CSV_DIALECT)
        writer.writerow(columns)
        writer.writerow([template.get(column, "") for column in columns])
        stream.flush()
        stream.detach()


def merge(packages: list[Package], dest: Path) -> MergeStats:
    """Scala paczki w jeden plik ZIP pod `dest`.

    Zakłada, że okna paczek są rozłączne — o to dba `gtfs.chain_from_today`.
    Paczki zachodzące na siebie zdublowałyby kursy w dniach wspólnych.
    """
    if not packages:
        raise MergeError("brak paczek do scalenia")
    if len(packages) == 1:
        raise MergeError("scalanie jednej paczki nie ma sensu — użyj innej polityki")

    ordered = sorted(packages, key=lambda package: (package.window.start, package.window.end))
    labels = _labels(ordered)
    window = FeedWindow(
        start=min(package.window.start for package in ordered),
        end=max(package.window.end for package in ordered),
    )
    stats = MergeStats()
    stamp = (window.start.year, window.start.month, window.start.day, 0, 0, 0)

    archives = [zipfile.ZipFile(package.path) for package in ordered]
    try:
        headers = _collect_headers(archives, ordered)
        dest.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(
            dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as out:
            for name in sorted(headers):
                if name in GENERATED:
                    continue
                header = headers[name]
                mode, key_column = _mode_for(name, header)
                _merge_table(
                    out, archives, labels, name, header, mode, key_column, stamp, stats
                )

            _write_feed_info(
                out,
                archives,
                headers.get("feed_info.txt", DEFAULT_FEED_INFO),
                window,
                labels,
                stamp,
            )
    finally:
        for archive in archives:
            archive.close()

    log.info(
        "scalono %d paczek (%s) -> %s, %.1f MB, %d kursów",
        len(ordered),
        "+".join(labels),
        dest.name,
        dest.stat().st_size / 1e6,
        stats.rows.get("trips.txt", 0),
    )
    for table, count in stats.conflicts.items():
        fields = stats.conflict_fields.get(table, Counter())
        log.warning(
            "%s: %d rekordów miało różne warianty między paczkami "
            "(wybrano najczęstszy); różnice w polach: %s",
            table,
            count,
            ", ".join(f"{column} x{number}" for column, number in fields.most_common()) or "—",
        )
    for table, count in stats.skipped.items():
        log.warning("%s: pominięto %d wierszy o złej liczbie kolumn", table, count)
    return stats


def _merge_table(
    out: zipfile.ZipFile,
    archives: list[zipfile.ZipFile],
    labels: list[str],
    name: str,
    header: list[str],
    mode: str,
    key_column: str | None,
    stamp: tuple[int, int, int, int, int, int],
    stats: MergeStats,
) -> None:
    prefix_indexes = [
        header.index(column) for column in PREFIXED_COLUMNS if column in header
    ]
    key_index = header.index(key_column) if key_column else -1

    if mode == "pk":
        rows = _vote(archives, name, header, key_index, stats)
        _write_rows(out, name, header, stamp, rows, stats)
        return

    # Porównujemy całe wiersze, nie skróty — tabele w trybie "row" są małe,
    # a skrót mógłby po cichu zgubić rekord.
    seen_rows: set[tuple[str, ...]] = set()
    emitted_groups: set[str] = set()

    with out.open(zip_entry(name, stamp), "w") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="", write_through=False)
        writer = csv.writer(stream, **CSV_DIALECT)
        writer.writerow(header)

        for archive, label in zip(archives, labels):
            if name not in archive.namelist():
                continue
            group_ids: set[str] = set()

            for row in _iter_rows(archive, name):
                if len(row) != len(header):
                    stats.skipped[name] += 1
                    continue

                if mode == "prefix":
                    for index in prefix_indexes:
                        row[index] = f"{label}_{row[index]}"

                elif mode == "group":
                    group = row[key_index]
                    if group in emitted_groups:
                        continue
                    # Do zbioru globalnego dopiero po całej paczce, żeby nie uciąć
                    # jej własnych kolejnych punktów tego samego kształtu.
                    group_ids.add(group)

                elif mode == "row":
                    current = tuple(row)
                    if current in seen_rows:
                        continue
                    seen_rows.add(current)

                writer.writerow(row)
                stats.rows[name] += 1

            emitted_groups |= group_ids

        stream.flush()
        stream.detach()


def _vote(
    archives: list[zipfile.ZipFile],
    name: str,
    header: list[str],
    key_index: int,
    stats: MergeStats,
) -> list[tuple[str, ...]]:
    """Dla każdego klucza wybiera wersję wiersza występującą w największej
    liczbie paczek. Przy remisie wygrywa napotkana pierwsza (Counter zachowuje
    kolejność wstawiania).

    Tabele w tym trybie mają setki albo tysiące wierszy, więc trzymanie ich
    w pamięci nic nie kosztuje — w odróżnieniu od stop_times.txt, które idzie
    strumieniowo.
    """
    votes: dict[str, Counter] = {}
    order: list[str] = []

    for archive in archives:
        if name not in archive.namelist():
            continue
        for row in _iter_rows(archive, name):
            if len(row) != len(header):
                stats.skipped[name] += 1
                continue
            key = row[key_index]
            if key not in votes:
                votes[key] = Counter()
                order.append(key)
            votes[key][tuple(row)] += 1

    chosen: list[tuple[str, ...]] = []
    for key in order:
        variants = votes[key]
        winner, _ = variants.most_common(1)[0]
        if len(variants) > 1:
            stats.conflicts[name] += 1
            for other in variants:
                if other == winner:
                    continue
                stats.conflict_fields.setdefault(name, Counter()).update(
                    column
                    for column, kept, dropped in zip(header, winner, other)
                    if kept != dropped
                )
        chosen.append(winner)
    return chosen


def _write_rows(
    out: zipfile.ZipFile,
    name: str,
    header: list[str],
    stamp: tuple[int, int, int, int, int, int],
    rows: list[tuple[str, ...]],
    stats: MergeStats,
) -> None:
    with out.open(zip_entry(name, stamp), "w") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="", write_through=False)
        writer = csv.writer(stream, **CSV_DIALECT)
        writer.writerow(header)
        writer.writerows(rows)
        stats.rows[name] += len(rows)
        stream.flush()
        stream.detach()
