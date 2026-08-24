"""Zaglądanie do środka paczki GTFS: walidacja, odczyt okna obowiązywania
i wybór paczek do publikacji.

Cel jest jeden: opublikowany feed ma obejmować dzisiejszy dzień i sięgać
możliwie daleko w przód. Stąd dwie polityki i nic więcej:

``single``
    Źródło daje jeden plik — bierzemy go. Tak działa większość miast.

``today_forward``
    Źródło daje wiele paczek — składamy z nich łańcuch: paczka obejmująca
    dzisiaj i sięgająca najdalej, a za nią kolejne, z których każda zaczyna
    się **dokładnie** dzień po końcu poprzedniej.

Warunek „dokładnie" jest tu istotny. Paczki GZM są rozłączne, więc łańcuch
spina wszystkie dziewięć w komplet 14 dni. Paczki Wrocławia nakładają się
w czasie, więc łańcuch kończy się na pierwszej — i dobrze, bo doklejenie
nakładającej się paczki zdublowałoby kursy w dniach wspólnych.

Okno czytamy z pliku, nie z nazwy ani z metadanych portalu — obu nie da się
ufać. Wrocław trzyma w nazwie datę początku, ale nic o końcu; CKAN podaje datę
wgrania zasobu, która z okresem obowiązywania nie ma nic wspólnego.
"""

from __future__ import annotations

import csv
import logging
import zlib
import zipfile
from datetime import date, timedelta
from pathlib import Path

from .config import Candidate, FeedWindow, Package
from .tables import read_table

log = logging.getLogger(__name__)

#: Minimum, bez którego plik nie jest GTFS-em i nie ma po co go publikować.
REQUIRED_FILES = ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt")

CALENDAR_FILES = ("calendar.txt", "calendar_dates.txt")

#: Ile paczek z przeszłości zostawić na krótkiej liście. Jedna wystarcza do
#: pokrycia dzisiaj, druga to zapas na wypadek, gdyby pierwsza była uszkodzona.
KEEP_PAST = 2

#: Ile paczek bez czytelnej daty zostawić, gdy inne tę datę mają. Zwykle są to
#: pozostałości po starszej konwencji nazw (Wrocław ma 23 takie z 69) i nie ma
#: sensu ich pobierać. Dwie zostają jako zabezpieczenie: gdyby źródło zmieniło
#: format nazwy, nowe pliki trafiłyby właśnie tutaj.
KEEP_UNDATED = 2


#: Wszystko, czym potrafi się zakończyć czytanie uszkodzonego archiwum.
#:
#: ``zipfile.testzip()`` łapie u siebie wyłącznie ``BadZipFile`` (czyli zły CRC),
#: ale rozsypany strumień deflate rzuca ``zlib.error`` i przelatuje wyżej.
#: Bez tej listy taki plik przerwałby cały przebieg, zostawiając pozostałe miasta
#: niepobrane — zamiast oznaczyć jedno źródło jako nieudane.
ARCHIVE_ERRORS = (zipfile.BadZipFile, zlib.error, csv.Error, EOFError, ValueError)


class GtfsError(Exception):
    """Pobrany plik nie jest używalną paczką GTFS."""


def _parse_gtfs_date(value: str | None) -> date | None:
    value = (value or "").strip()
    if len(value) != 8 or not value.isdigit():
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None


def read_window(path: Path) -> FeedWindow:
    """Zwraca okno obowiązywania: najpierw feed_info.txt, w razie braku min/max
    z kalendarzy."""
    with zipfile.ZipFile(path) as archive:
        for row in read_table(archive, "feed_info.txt"):
            start = _parse_gtfs_date(row.get("feed_start_date"))
            end = _parse_gtfs_date(row.get("feed_end_date"))
            if start and end:
                return FeedWindow(start, end)

        dates: list[date] = []
        for row in read_table(archive, "calendar.txt"):
            dates.extend(
                found
                for found in (
                    _parse_gtfs_date(row.get("start_date")),
                    _parse_gtfs_date(row.get("end_date")),
                )
                if found
            )
        for row in read_table(archive, "calendar_dates.txt"):
            found = _parse_gtfs_date(row.get("date"))
            if found:
                dates.append(found)

    if not dates:
        raise GtfsError(f"{path.name}: nie da się ustalić okresu obowiązywania feedu")
    return FeedWindow(min(dates), max(dates))


def validate(path: Path) -> FeedWindow:
    """Sprawdza, czy plik nadaje się do publikacji, i zwraca jego okno.
    Rzuca GtfsError — wtedy wołający zostawia poprzednią wersję na miejscu."""
    if not zipfile.is_zipfile(path):
        raise GtfsError(f"{path.name}: to nie jest archiwum ZIP")

    try:
        with zipfile.ZipFile(path) as archive:
            broken = archive.testzip()
            if broken is not None:
                raise GtfsError(f"{path.name}: uszkodzony wpis w archiwum ({broken})")

            names = set(archive.namelist())
            missing = [required for required in REQUIRED_FILES if required not in names]
            if missing:
                raise GtfsError(f"{path.name}: brakuje plików GTFS: {', '.join(missing)}")
            if not any(calendar in names for calendar in CALENDAR_FILES):
                raise GtfsError(f"{path.name}: brak calendar.txt i calendar_dates.txt")

        return read_window(path)
    except ARCHIVE_ERRORS as exc:
        raise GtfsError(f"{path.name}: uszkodzone archiwum ({exc})") from exc


def shortlist(candidates: list[Candidate], today: date, limit: int) -> list[Candidate]:
    """Ogranicza listę do paczek, które mogą wejść do łańcucha od dzisiaj.

    Bez tego polityka today_forward pobrałaby wszystkie 69 plików Wrocławia
    (760 MB), żeby po odczytaniu ich okien wyrzucić 66. Data z nazwy pliku
    (``hint_start``) pozwala odsiać archiwum, zanim cokolwiek pobierzemy.
    Paczki bez podpowiedzi — jak u GZM — przechodzą w całości, bo o nich
    z góry nie wiadomo nic.
    """
    past = sorted(
        (c for c in candidates if c.hint_start and c.hint_start <= today),
        key=lambda c: c.hint_start,
        reverse=True,
    )[:KEEP_PAST]
    future = sorted(
        (c for c in candidates if c.hint_start and c.hint_start > today),
        key=lambda c: c.hint_start,
    )
    undated = [c for c in candidates if not c.hint_start]

    # Gdy żadna paczka nie ma daty w nazwie (tak jest u GZM), o żadnej nie wiadomo
    # nic z góry i trzeba wziąć wszystkie. Gdy część ma, reszta to archiwum.
    if past or future:
        undated = undated[:KEEP_UNDATED]

    picked = past + future + undated
    dropped = len(candidates) - len(picked)
    if dropped:
        log.info("odsiano %d z %d paczek na podstawie daty w nazwie", dropped, len(candidates))
    if len(picked) > limit:
        log.warning(
            "pobieram %d z %d wytypowanych paczek — reszta pominięta (limit MAX_DOWNLOADS)",
            limit,
            len(picked),
        )
        picked = picked[:limit]
    return picked


def chain_from_today(packages: list[Package], today: date) -> list[Package]:
    """Buduje łańcuch paczek pokrywający dzisiaj i możliwie dużo dni w przód.

    Zwraca pustą listę, gdy żadna paczka nie obejmuje dzisiejszej daty.
    """
    remaining = list(packages)

    covering = [package for package in remaining if package.window.covers(today)]
    if not covering:
        return []

    # Spośród obejmujących dzisiaj bierzemy tę, która sięga najdalej.
    first = max(covering, key=lambda package: package.window.end)
    chain = [first]
    remaining.remove(first)
    day = first.window.end + timedelta(days=1)

    # Dalej doklejamy wyłącznie paczki stykające się co do dnia. Paczka
    # zachodząca na już wybrany zakres zdublowałaby kursy, a paczka zaczynająca
    # się później zostawiłaby dziurę w rozkładzie.
    while True:
        following = [package for package in remaining if package.window.start == day]
        if not following:
            break
        best = max(following, key=lambda package: package.window.end)
        chain.append(best)
        remaining.remove(best)
        day = best.window.end + timedelta(days=1)

    return chain
