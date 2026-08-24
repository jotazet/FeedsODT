"""Współrzędne peronów kolejowych z OSM.

Przewoźnicy podają w GTFS jeden punkt na stację i przypisują go wszystkim
peronom. W feedzie POLREGIO peron I/1, peron II/2 i sama stacja mają
identyczne ``stop_lat``/``stop_lon`` — pasażer na mapie widzi trzy pinezki
w jednym miejscu, a planer nie potrafi pokazać, na który peron ma iść.

Źródłem prawdziwych współrzędnych jest lista peronów PLK zbudowana z OSM:
https://kasmar00.github.io/osm-plk-platform-validator/platforms-list.json

Format: ``{slug_stacji: [{station_name, platform, track, location: [lon, lat],
operator, exact_location, single_track_platform}, ...]}``.

Dwie rzeczy, które trzeba w tym pliku obejść:

**Kluczy nie da się użyć do wyszukiwania.** Slug powstaje przez podmianę
*małych* polskich liter na ASCII i dopiero potem lowercase, więc wielkie
litery diakrytyczne wypadają: ``Bardo Śląskie`` → ``bardo-laskie``,
``Bydgoszcz Łęgnowo`` → ``bydgoszcz-egnowo``. Dopasowanie po kluczu gubi
239 stacji. Dopasowujemy więc po ``station_name`` ze złożeniem wszystkich
diakrytyków — wtedy z 2220 stacji POLREGIO trafia 2050.

**Numeracja peronów jest inna niż w GTFS.** Lista podaje peron arabski
(``"2"``), a POLREGIO rzymski, razem z torem: ``platform_code = "II/3"``
znaczy peron II, tor 3. Stąd translacja przed wyszukaniem.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Iterable

log = logging.getLogger(__name__)

#: Wersjonowana kopia w repozytorium — używana, gdy odświeżenie z sieci zawiedzie.
#: Feed peronów zmienia się rzadko (to zrzut z OSM), więc wczorajsza lista jest
#: w zupełności dobra, a brak sieci nie może wywrócić przebiegu.
BUNDLED = Path(__file__).resolve().parent / "data" / "platforms-list.json"

PLATFORMS_URL = "https://kasmar00.github.io/osm-plk-platform-validator/platforms-list.json"

#: Poniżej tylu stacji plik uznajemy za obcięty i nie nadpisujemy nim kopii.
#: Pełna lista ma ok. 3000 stacji; 2000 zostawia zapas na naturalne wahania.
MIN_STATIONS = 2000

#: Ile miejsc po przecinku zapisujemy. 6 to ok. 11 cm — więcej nie ma sensu,
#: bo i tak są to obrysy peronów z OSM, a nie pomiar geodezyjny.
PRECISION = 6

_TRANSLIT = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")

_ROMAN = {"I": 1, "V": 5, "X": 10}

#: ``II/3``, ``IIIa/5``, ``I/1a``, ``V/4r``, ``II`` (sam peron, bez toru).
#: Litera po numerze peronu jest częścią jego oznaczenia (peron 1a to nie
#: peron 1); litera po numerze toru to zwykle dopisek techniczny i ją pomijamy.
#:
#: Peron bywa podany od razu arabski — POLREGIO pisze rzymskimi, ale Koleje
#: Małopolskie mają w jednym miejscu „peron 5". Lista PLK i tak trzyma arabskie,
#: więc taki zapis wchodzi bez konwersji.
_PLATFORM_CODE = re.compile(r"^(?:([IVX]+)|(\d+))([a-z]?)(?:/(\d+)[a-zA-Z]*)?$")


class PlatformError(Exception):
    """Listy peronów nie da się wczytać — ani z sieci, ani z kopii."""


@dataclass(frozen=True)
class Location:
    """Znalezione współrzędne peronu i to, jak dokładnie je znaleziono."""

    lat: float
    lon: float
    #: ``track`` — trafiony peron i tor; ``platform`` — sam peron (średnia
    #: z jego torów, bo w oznaczeniu POLREGIO toru nie było albo się nie zgadzał).
    quality: str


def normalize(name: str) -> str:
    """Nazwa stacji -> klucz porównania: ``Bardo Śląskie`` i ``BARDO SLASKIE``
    dają to samo. Składa diakrytyki, zbija wszystko poza [a-z0-9] do spacji."""
    return re.sub(r"[^a-z0-9]+", " ", name.translate(_TRANSLIT).lower()).strip()


def _from_roman(value: str) -> str | None:
    total = 0
    previous = 0
    for char in reversed(value):
        digit = _ROMAN.get(char)
        if digit is None:
            return None
        total = total - digit if digit < previous else total + digit
        previous = max(previous, digit)
    return str(total) if total else None


def parse_platform_code(code: str) -> tuple[str, str | None] | None:
    """``"II/3"`` -> ``("2", "3")``, ``"IIIa"`` -> ``("3a", None)``.

    Zwraca ``None``, gdy oznaczenie nie jest peronem — tak jest dla ``BUS``
    (przystanek komunikacji zastępczej, peronu nie ma) i dla pustego pola.
    """
    match = _PLATFORM_CODE.match(code.strip())
    if not match:
        return None
    roman, arabic, suffix, track = match.groups()
    number = arabic if arabic else _from_roman(roman)
    if number is None:
        return None
    return number + suffix, track


class PlatformIndex:
    """Lista peronów gotowa do odpytywania nazwą stacji i oznaczeniem peronu."""

    def __init__(self, stations: dict[str, list[dict]]) -> None:
        self._stations = stations

    def __len__(self) -> int:
        return len(self._stations)

    @classmethod
    def from_json(cls, raw: str) -> PlatformIndex:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise PlatformError("lista peronów nie jest obiektem JSON")
        stations: dict[str, list[dict]] = {}
        for entries in data.values():
            if not entries:
                continue
            # 61 peronów w liście ma location: null — OSM zna ich istnienie,
            # ale nie ma obrysu. Bez współrzędnych są dla nas bezużyteczne.
            located = [entry for entry in entries if _is_point(entry.get("location"))]
            if not located:
                continue
            # Klucz z pliku pomijamy świadomie — patrz docstring modułu.
            stations.setdefault(normalize(entries[0]["station_name"]), []).extend(located)
        if not stations:
            raise PlatformError("lista peronów jest pusta")
        return cls(stations)

    @classmethod
    def load(cls, cache: Path | None = None, session=None) -> PlatformIndex:
        """Wczytuje listę: najpierw odświeżenie z sieci, potem kopia w cache,
        na końcu kopia z repozytorium. Nigdy nie kończy się błędem, dopóki
        istnieje kopia wersjonowana.

        Wynik jest zapamiętywany na sesji HTTP. Runner zakłada jedną sesję na
        przebieg, więc lista schodzi z sieci raz, choćby korzystało z niej kilka
        źródeł — a nie odkłada się na stałe w procesie demona, który żyje
        tygodniami i ma widzieć jutrzejsze poprawki w OSM.
        """
        if session is not None:
            memo = getattr(session, "_feedsodt_platforms", None)
            if memo is not None:
                return memo
            fresh = cls._refresh(cache, session)
            if fresh is not None:
                session._feedsodt_platforms = fresh
                return fresh

        for path in (cache, BUNDLED):
            if path is None or not path.is_file():
                continue
            try:
                index = cls.from_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError) as exc:
                log.warning("perony: %s nieczytelne (%s)", path, exc)
                continue
            log.info("perony: %d stacji z %s", len(index), path)
            if session is not None:
                session._feedsodt_platforms = index
            return index

        raise PlatformError(f"brak listy peronów — nie ma nawet {BUNDLED}")

    @classmethod
    def _refresh(cls, cache: Path | None, session) -> PlatformIndex | None:
        try:
            response = session.get(PLATFORMS_URL, timeout=30)
            response.raise_for_status()
            index = cls.from_json(response.text)
        except Exception as exc:  # sieć, HTTP, JSON — reakcja i tak ta sama
            log.warning("perony: odświeżenie nieudane (%s), biorę kopię", exc)
            return None

        if len(index) < MIN_STATIONS:
            log.warning(
                "perony: pobrana lista ma tylko %d stacji (< %d) — wygląda na obciętą, "
                "zostaję przy kopii",
                len(index),
                MIN_STATIONS,
            )
            return None

        log.info("perony: %d stacji pobranych z %s", len(index), PLATFORMS_URL)
        if cache is not None:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(response.text, encoding="utf-8")
            except OSError as exc:
                log.warning("perony: nie udało się zapisać kopii w %s (%s)", cache, exc)
        return index

    def station(self, station_name: str) -> Location | None:
        """Środek stacji — uśredniony ze wszystkich jej peronów.

        Ratunek dla przystanków, którym przewoźnik nie podał współrzędnych:
        w feedzie KML SKA stacja BAŃSKA NIŻNA ma zapisane 0,0, czyli punkt
        w zatoce Gwinejskiej.
        """
        entries = self._stations.get(normalize(station_name))
        return _centroid(entries, "station") if entries else None

    def proper_name(self, station_name: str) -> str | None:
        """Nazwa stacji w zapisie z OSM. Część feedów pisze je wersalikami
        (``KRAKÓW GŁÓWNY``), a lista PLK trzyma poprawną wielkość liter."""
        entries = self._stations.get(normalize(station_name))
        return entries[0]["station_name"].strip() if entries else None

    def locate(self, station_name: str, platform_code: str) -> Location | None:
        """Współrzędne peronu albo ``None``, gdy nie da się go wskazać."""
        entries = self._stations.get(normalize(station_name))
        if not entries:
            return None
        parsed = parse_platform_code(platform_code)
        if parsed is None:
            return None
        platform, track = parsed

        on_platform = [entry for entry in entries if entry["platform"] == platform]
        if not on_platform:
            return None

        if track is not None:
            exact = [entry for entry in on_platform if entry.get("track") == track]
            if exact:
                return _centroid(exact, "track")

        # Tor się nie zgadza albo POLREGIO go nie podał. Środek peronu jest
        # wciąż o rząd wielkości lepszy niż punkt stacji — perony bywają
        # oddalone od budynku dworca o setki metrów.
        return _centroid(on_platform, "platform")


def _is_point(location) -> bool:
    return (
        isinstance(location, (list, tuple))
        and len(location) == 2
        and all(isinstance(value, (int, float)) for value in location)
    )


def _centroid(entries: Iterable[dict], quality: str) -> Location:
    points = [entry["location"] for entry in entries]
    return Location(
        lat=round(fmean([point[1] for point in points]), PRECISION),
        lon=round(fmean([point[0] for point in points]), PRECISION),
        quality=quality,
    )
