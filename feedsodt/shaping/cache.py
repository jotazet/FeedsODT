"""Cache geometrii kształtów między przebiegami.

Routowanie 4,5 tys. sekwencji przystanków POLREGIO zajmuje kilka minut,
a feed obowiązuje do grudnia i między publikacjami zmienia się w nim niewiele.
Bez cache'a każdy przebieg liczyłby od zera to samo.

Kluczem jest skrót *współrzędnych* sekwencji, a nie identyfikatorów
przystanków: gdy lista peronów PLK poprawi położenie peronu, unieważnione
zostaną dokładnie te kształty, które przez ten peron prowadzą, i nic poza nimi.

Pamiętamy też **nieudane** próby. 39 sekwencji POLREGIO nie da się zroutować,
bo wychodzą poza wycinek OSM (Berlin, Cottbus, Bützow) — a każda taka próba to
kaskada trzech zapytań, z których każde czeka na timeout. Bez cache'a
negatywnego kosztowały minutę na przebieg, w kółko, przez cały rok. Wpis
o porażce przeterminowuje się po ``RETRY_AFTER_DAYS``, więc podmiana wycinka
OSM na szerszy sama się w końcu odbije w wyniku.

SQLite, bo jest w bibliotece standardowej, znosi przerwany zapis i nie wymaga
wczytywania całości do pamięci przy 4,5 tys. wielopunktowych linii.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

#: Po ilu dniach ponawiamy sekwencję, której nie udało się zroutować.
#: Tydzień: dość rzadko, żeby nie płacić za nią w każdym przebiegu, i dość
#: często, żeby poszerzenie wycinka OSM albo naprawa torów w OSM weszły same.
RETRY_AFTER_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS shapes (
    key    TEXT PRIMARY KEY,
    method TEXT NOT NULL,
    coords TEXT NOT NULL,
    stamp  INTEGER NOT NULL DEFAULT 0
)
"""


@dataclass(frozen=True)
class Lookup:
    """Wynik zajrzenia do cache'a."""

    #: Geometria, jeśli jest znana.
    coords: list[list[float]] | None = None
    #: True, gdy wiemy, że tej sekwencji nie da się zroutować, i jest jeszcze
    #: za wcześnie na kolejną próbę.
    known_bad: bool = False

    @property
    def hit(self) -> bool:
        return self.coords is not None


class ShapeCache:
    """Mapa ``klucz -> geometria``.

    Wariant bez pliku (``None``) trzyma to samo w słowniku w pamięci, więc
    wołający zawsze może czytać geometrię z cache'a zamiast trzymać ją u siebie
    — a to właśnie pozwala nie mieć nigdy więcej niż jednego kształtu naraz.
    """

    def __init__(self, connection: sqlite3.Connection | None, today: date | None = None) -> None:
        self._connection = connection
        self._memory: dict[str, tuple[str, int]] = {}
        self._today = (today or date.today()).toordinal()

    @classmethod
    @contextlib.contextmanager
    def open(cls, path: Path | None, today: date | None = None) -> Iterator[ShapeCache]:
        if path is None:
            yield cls(None, today)
            return

        connection = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path)
            connection.execute(SCHEMA)
            _migrate(connection)
        except (OSError, sqlite3.Error) as exc:
            # Zepsuty albo niezapisywalny cache nie może wywrócić przebiegu —
            # najwyżej policzymy wszystko od nowa.
            log.warning("cache kształtów %s niedostępny (%s), liczę bez niego", path, exc)
            if connection is not None:
                connection.close()
            yield cls(None, today)
            return

        try:
            yield cls(connection, today)
        finally:
            connection.close()

    def lookup(self, key: str) -> Lookup:
        if self._connection is None:
            row = self._memory.get(key)
        else:
            try:
                row = self._connection.execute(
                    "SELECT coords, stamp FROM shapes WHERE key = ?", (key,)
                ).fetchone()
            except sqlite3.Error:
                return Lookup()
        if row is None:
            return Lookup()

        raw, stamp = row
        if not raw:
            return Lookup(known_bad=self._today - stamp < RETRY_AFTER_DAYS)
        try:
            return Lookup(coords=json.loads(raw))
        except ValueError:
            return Lookup()

    def put(self, key: str, coords: list[list[float]], method: str) -> None:
        self._write(key, json.dumps(coords, separators=(",", ":")), method)

    def put_failure(self, key: str, method: str) -> None:
        """Zapamiętuje, że sekwencji nie da się zroutować — pusta geometria
        z dzisiejszą datą."""
        self._write(key, "", method)

    def _write(self, key: str, payload: str, method: str) -> None:
        if self._connection is None:
            self._memory[key] = (payload, self._today)
            return
        try:
            self._connection.execute(
                "INSERT OR REPLACE INTO shapes (key, method, coords, stamp) VALUES (?, ?, ?, ?)",
                (key, method, payload, self._today),
            )
        except sqlite3.Error as exc:
            log.warning("cache kształtów: zapis nieudany (%s)", exc)

    def commit(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.commit()
        except sqlite3.Error as exc:
            log.warning("cache kształtów: commit nieudany (%s)", exc)


def _migrate(connection: sqlite3.Connection) -> None:
    """Dokłada kolumnę `stamp` do cache'ów sprzed cache'owania porażek."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(shapes)")}
    if "stamp" not in columns:
        connection.execute("ALTER TABLE shapes ADD COLUMN stamp INTEGER NOT NULL DEFAULT 0")
        connection.commit()
