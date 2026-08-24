"""Tabele GTFS wewnątrz archiwum: odczyt, zapis i stała data wpisu.

Trzy funkcje, których potrzebuje każdy krok przepisujący feed — scalanie,
przebudowy przewoźników i dorabianie geometrii. Wcześniej każdy z tych modułów
miał własną, identyczną kopię czytnika; leżą tu raz, żeby poprawka trafiała
w jedno miejsce.
"""

from __future__ import annotations

import csv
import io
import zipfile

#: `lineterminator` wymuszony, bo domyślne `\r\n` csv.writera dawałoby inne
#: bajty niż to, co przychodzi od przewoźników, a na bajtach opiera się
#: wykrywanie zmian.
CSV_DIALECT = {"lineterminator": "\n"}


def zip_entry(name: str, stamp: tuple[int, int, int, int, int, int]) -> zipfile.ZipInfo:
    """Wpis ZIP ze stałą datą.

    Bez tego zipfile wstawiłby bieżący czas, więc dwa przebiegi z identycznego
    wejścia dałyby pliki o różnych sha256 — a na sha256 opiera się wykrywanie,
    czy w ogóle jest co publikować.
    """
    info = zipfile.ZipInfo(name, date_time=stamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def read_table(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    """Czyta tabelę GTFS. Pusta lub nieobecna tabela to pusta lista, nie wyjątek —
    GTFS dopuszcza np. puste calendar.txt, gdy wszystko siedzi w calendar_dates."""
    if name not in archive.namelist():
        return []
    raw = archive.read(name).decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(raw)))


def write_table(
    out: zipfile.ZipFile,
    name: str,
    columns: tuple[str, ...],
    rows: list[dict[str, str]],
    stamp: tuple[int, ...],
) -> None:
    """Zapisuje tabelę ze wskazanym zestawem kolumn.

    ``extrasaction="ignore"``, bo wiersze przychodzą z oryginału i niosą pola,
    których w wyjściu nie chcemy — a nie jest to powód do wyjątku.
    """
    with out.open(zip_entry(name, stamp), "w") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore", **CSV_DIALECT)
        writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        stream.detach()
