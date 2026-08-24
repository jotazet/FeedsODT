"""Przebudowa feedu przewoźnika na feed wystawiany przez ODT.

Wersja lekka, sterowana konfiguracją, dla źródeł, które są poprawne — brakuje
im tylko dwóch rzeczy, których przewoźnik nie uzupełnia:

**Kolory linii.** ``route_color`` przychodzi puste w komplecie: 28/28 tras
w Olkuszu, 70/70 w MLD, 26/26 w ZKKM, 8/8 w SKA. Planer rysuje wtedy wszystko
jedną barwą i pasażer nie odróżnia linii od linii.

**Wystawca.** ``feed_info.txt`` podaje kiedyPrzyjedzie.pl albo R&G PLUS, czyli
firmę, która zrobiła rozkład dla przewoźnika. Konsument opublikowanej przez nas
paczki ma pisać do nas — patrz :mod:`feedsodt.feedinfo`.

Reszta tabel idzie bajt w bajt. To celowo *nie* jest miejsce na naprawianie
konkretnego przewoźnika: gdy feed wymaga przebudowy relacji, przystanków czy
kierunków, pisze się osobną transformację, jak ``polregio``.

Konfiguracja w bloku ``[[source]]``::

    transform    = "odt"
    route_color  = "0462A2"        # jedna barwa dla wszystkich linii
    colors_file  = "mld-routes.txt"  # albo barwa per linia, z config/colors/

``colors_file`` to zwykły ``routes.txt`` — ten sam plik, który dostaje się od
organizatora. Dopasowanie idzie po ``route_short_name``, bo ``route_id`` bywa
wersjonowany i zmienia się przy każdej publikacji (MLD: ``1359_A1`` -> ``1360_A1``).
"""

from __future__ import annotations

import csv
import io
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import feedinfo
from ..colors import ColorError, normalize, text_color_for
from ..tables import CSV_DIALECT, read_table, zip_entry
from . import Context, TransformError, transform

log = logging.getLogger(__name__)

#: Tabele przepisywane od nowa; reszta jest kopiowana bajt w bajt.
REBUILT = ("routes.txt", "feed_info.txt")

#: Pola, po których szukamy linii w tabeli kolorów — w tej kolejności.
#: ``route_id`` jest ostatni, bo u części przewoźników zawiera numer wersji
#: publikacji i zmienia się co tydzień.
MATCH_FIELDS = ("route_short_name", "route_long_name", "route_id")

@dataclass
class OdtStats:
    routes: int = 0
    colored_from_file: int = 0
    colored_default: int = 0
    kept_original: int = 0
    dropped_files: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


def load_colors(path: Path) -> dict[str, str]:
    """Wczytuje barwy z pliku w formacie ``routes.txt``.

    Klucz to ``route_short_name`` (a gdy pusty — ``route_long_name``), wartość
    to znormalizowany ``route_color``. Wiersze bez barwy są pomijane, żeby dało
    się wrzucić plik od organizatora bez czyszczenia go z braków, a wiersze
    zaczynające się od ``#`` — żeby dało się dopisać w nim komentarz.
    """
    if not path.is_file():
        raise TransformError(f"nie znaleziono tabeli kolorów: {path}")
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    body = "\n".join(
        line for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    colors: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(body)):
        key = (row.get("route_short_name") or row.get("route_long_name") or "").strip()
        value = (row.get("route_color") or "").strip()
        if not key or not value:
            continue
        try:
            colors[key] = normalize(value)
        except ColorError as exc:
            raise TransformError(f"{path.name}, linia {key!r}: {exc}") from None
    if not colors:
        raise TransformError(f"{path}: nie ma ani jednej linii z kolorem")
    return colors


@transform("odt")
def rebuild(source: Path, dest: Path, ctx: Context | None = None) -> OdtStats:
    """Koloruje trasy i przepisuje feed_info na ODT."""
    ctx = ctx or Context()
    stats = OdtStats()

    default = ctx.options.get("route_color")
    default_color = normalize(str(default)) if default else ""

    colors: dict[str, str] = {}
    colors_file = ctx.options.get("colors_file")
    if colors_file:
        colors = load_colors(ctx.colors_dir / str(colors_file))

    if not colors and not default_color:
        raise TransformError(
            "transform = \"odt\" wymaga route_color albo colors_file w sources.toml — "
            "bez żadnego z nich nie ma czego zmieniać"
        )

    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        missing = [
            name for name in ("routes.txt", "trips.txt", "stops.txt") if name not in names
        ]
        if missing:
            raise TransformError(f"{source.name}: brakuje {', '.join(missing)}")

        routes = read_table(archive, "routes.txt")
        if not routes:
            raise TransformError(f"{source.name}: routes.txt jest puste")

        window = _window(archive)
        stamp = (int(window[0][:4]), int(window[0][4:6]), int(window[0][6:8]), 0, 0, 0)
        colored = _color(routes, colors, default_color, stats)

        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
            for name in sorted(names):
                if name.endswith("/") or name in REBUILT:
                    continue
                if not name.endswith(".txt"):
                    # Część feedów jest pakowana Eksplorerem albo Excelem
                    # i niesie [Content_Types].xml, którego w GTFS-ie nie ma.
                    stats.dropped_files.append(name)
                    continue
                with archive.open(name) as src, out.open(zip_entry(name, stamp), "w") as target:
                    shutil.copyfileobj(src, target, 256 * 1024)

            _write_routes(out, colored, stamp)
            feedinfo.write(
                out,
                zip_entry("feed_info.txt", stamp),
                window,
                f"feedsodt:{window[0]}-{window[1]}:{len(routes)}",
            )

    log.info(
        "odt: %d tras — %d pokolorowanych z tabeli, %d domyślnym, %d zostawionych bez zmian",
        stats.routes,
        stats.colored_from_file,
        stats.colored_default,
        stats.kept_original,
    )
    if stats.unmatched:
        log.warning(
            "odt: %d linii bez wpisu w tabeli kolorów: %s",
            len(stats.unmatched),
            ", ".join(stats.unmatched[:10]) + ("…" if len(stats.unmatched) > 10 else ""),
        )
    if stats.dropped_files:
        log.info("odt: pominięto pliki spoza GTFS: %s", ", ".join(stats.dropped_files))
    return stats


def _color(
    routes: list[dict[str, str]],
    colors: dict[str, str],
    default_color: str,
    stats: OdtStats,
) -> list[dict[str, str]]:
    """Nadaje barwy: tabela wygrywa nad domyślną, domyślna nad tym, co było."""
    stats.routes = len(routes)
    result = []
    for route in routes:
        color = ""
        for column in MATCH_FIELDS:
            found = colors.get((route.get(column) or "").strip())
            if found:
                color = found
                stats.colored_from_file += 1
                break
        if not color:
            label = (route.get("route_short_name") or route.get("route_id") or "?").strip()
            if colors:
                stats.unmatched.append(label)
            if default_color:
                color = default_color
                stats.colored_default += 1
            else:
                existing = (route.get("route_color") or "").strip()
                if existing:
                    color = normalize(existing)
                    stats.kept_original += 1
        route = dict(route)
        route["route_color"] = color
        route["route_text_color"] = text_color_for(color) if color else ""
        result.append(route)
    return result


def _window(archive: zipfile.ZipFile) -> tuple[str, str]:
    """Okno obowiązywania: z feed_info.txt, a gdy go nie ma — z kalendarzy."""
    for row in read_table(archive, "feed_info.txt"):
        start = (row.get("feed_start_date") or "").strip()
        end = (row.get("feed_end_date") or "").strip()
        if _is_date(start) and _is_date(end):
            return start, end

    dates: list[str] = []
    for row in read_table(archive, "calendar.txt"):
        dates += [row.get("start_date", ""), row.get("end_date", "")]
    for row in read_table(archive, "calendar_dates.txt"):
        dates.append(row.get("date", ""))
    dates = [value.strip() for value in dates if _is_date((value or "").strip())]
    if not dates:
        raise TransformError("nie da się ustalić okresu obowiązywania feedu")
    return min(dates), max(dates)


def _is_date(value: str) -> bool:
    return len(value) == 8 and value.isdigit()


def _write_routes(
    out: zipfile.ZipFile, routes: list[dict[str, str]], stamp: tuple[int, ...]
) -> None:
    """Zapisuje routes.txt z zachowaniem kolumn oryginału — feedy niosą tu
    pola, których nie rozumiemy (route_sort_order, continuous_pickup), i nie
    ma powodu ich gubić."""
    columns = list(routes[0].keys())
    for required in ("route_color", "route_text_color"):
        if required not in columns:
            columns.append(required)
    with out.open(zip_entry("routes.txt", stamp), "w") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore", **CSV_DIALECT)
        writer.writeheader()
        writer.writerows(routes)
        stream.flush()
        stream.detach()
