"""Konfiguracja i wspólne typy danych.

Wczytanie sources.toml i zmiennych środowiskowych, plus trzy struktury, którymi
posługują się wszystkie pozostałe moduły: `Candidate` (co jest do wzięcia),
`FeedWindow` (na kiedy obowiązuje) i `Package` (pobrane i sprawdzone).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

#: single       — źródło daje jeden plik, bierzemy go
#: today_forward — składamy łańcuch paczek: dzisiaj + jak najdalej w przód
VALID_POLICIES = frozenset({"single", "today_forward"})

#: Godzina przebiegu: HH:MM, 24-godzinnie.
_RUN_AT = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

#: Początek liczenia cyklu wielodniowego. Stała data, a nie „dzień pierwszego
#: uruchomienia": dzięki temu numer dnia w cyklu jest ten sam po restarcie
#: kontenera, po przeprowadzce na inną maszynę i po przerwie w działaniu.
CYCLE_ANCHOR = date(2026, 1, 1)


class ConfigError(Exception):
    """Konfiguracja jest niepoprawna — program nie powinien wystartować."""


@dataclass(frozen=True)
class SourceConfig:
    """Jedno źródło = jeden plik wyjściowy."""

    name: str
    output: str
    resolver: str
    select: str
    enabled: bool = True
    #: Opcjonalna przebudowa feedu po pobraniu, np. "polregio". Puste = publikujemy
    #: to, co dał przewoźnik, bajt w bajt.
    transform: str = ""
    #: Opcjonalne dorobienie shapes.txt: lista trybów, np. "rail,bus".
    #: Puste = nie ruszamy geometrii.
    shape: str = ""
    #: Obszar pokryty wycinkiem OSM, np. "poland". Kurs, który z niego
    #: wyjeżdża, zostaje bez kształtu — router poza wycinkiem nie odmawia,
    #: tylko dosuwa przystanek do najbliższego miejsca, które zna.
    shape_area: str = ""
    #: Własna godzina przebiegu (HH:MM). Puste = wspólna FEEDSODT_RUN_AT.
    #: Źródło kształtowane liczy się minutami, więc nie ma powodu, żeby
    #: blokowało nim osiem miast, które pobierają się w sekundy.
    run_at: str = ""
    #: Dzień w cyklu (0, 1, 2… przy FEEDSODT_INTERVAL_DAYS=3). Razem z run_at
    #: daje jeden slot na źródło — nigdy dwa naraz na tej samej maszynie.
    run_day: int = 0
    #: pozostałe klucze z bloku [[source]], interpretowane przez konkretny resolver
    options: dict[str, object] = field(default_factory=dict)

    def schedule(self, default: str) -> str:
        return self.run_at or default

    def slot(self, interval_days: int, default: str) -> tuple[int, str]:
        """Miejsce źródła w cyklu: (dzień, godzina)."""
        return self.run_day % max(interval_days, 1), self.schedule(default)

    def option(self, key: str) -> str:
        """Zwraca wymagane pole resolvera albo mówi wprost, czego brakuje i gdzie."""
        value = self.options.get(key)
        if not value:
            raise ConfigError(
                f"źródło {self.name!r} (resolver {self.resolver!r}) wymaga pola {key!r} w sources.toml"
            )
        return str(value)


@dataclass(frozen=True)
class Candidate:
    """Plik możliwy do pobrania, tak jak widzi go resolver — jeszcze bez zaglądania do środka."""

    url: str
    filename: str
    #: Zgadywany początek obowiązywania, jeśli resolver umie go wyczytać z nazwy pliku.
    #: Służy wyłącznie do ustawienia kolejności sprawdzania — prawdziwe okno i tak
    #: czytamy z feed_info.txt po pobraniu. Bez tego trzeba by ściągnąć wszystkie
    #: 69 plików Wrocławia, żeby dowiedzieć się, który obowiązuje dziś.
    hint_start: date | None = None
    #: Basic auth, gdy plik jest za nim schowany (publiczny share Nextcloud:
    #: token jako login, puste hasło). Trzymane osobno, żeby nie wyciekło
    #: do source_url w index.json.
    auth: tuple[str, str] | None = None


@dataclass(frozen=True)
class FeedWindow:
    """Zakres obowiązywania feedu, odczytany z wnętrza ZIP-a."""

    start: date
    end: date

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end

    def __str__(self) -> str:
        return f"{self.start:%Y-%m-%d}..{self.end:%Y-%m-%d}"


@dataclass(frozen=True)
class Package:
    """Paczka pobrana i sprawdzona: skąd pochodzi, na jaki okres obowiązuje
    i gdzie leży na dysku.

    Te trzy rzeczy zawsze wędrują razem, więc trzymamy je razem. Wcześniej były
    krotką `(Candidate, FeedWindow)` i osobnym słownikiem ścieżek indeksowanym
    po URL-u, co dawało zapisy w rodzaju `paths[chain[0][0].url]`.
    """

    candidate: Candidate
    window: FeedWindow
    path: Path

    @property
    def filename(self) -> str:
        return self.candidate.filename

    @property
    def url(self) -> str:
        return self.candidate.url


@dataclass(frozen=True)
class AppConfig:
    sources: tuple[SourceConfig, ...]
    output_dir: Path
    work_dir: Path
    #: Katalog na dane przeżywające przebieg: lista peronów, cache geometrii.
    #: Osobny od work_dir, bo work_dir jest czyszczony przy każdym źródle.
    cache_dir: Path
    #: Katalog z sources.toml — obok niego leżą tabele kolorów tras.
    config_dir: Path
    run_at: str
    #: Co ile dni odpytujemy źródła. 1 = codziennie.
    interval_days: int
    run_on_start: bool
    timezone: str
    #: Adresy silników routujących (docker-compose, profil "shape").
    rail_url: str = "http://railrouting:8989"
    bus_url: str = "http://graphhopper-bus:8989"
    shape_workers: int = 8
    shape_gps_accuracy: int = 50
    #: Odstępy (w minutach) kolejnych prób dla źródeł, które zawiodły.
    retry_minutes: tuple[int, ...] = (15, 30, 60)

    def cycle_day(self, today: date) -> int:
        """Który to dzień cyklu — liczony od stałej daty, nie od startu."""
        return (today.toordinal() - CYCLE_ANCHOR.toordinal()) % max(self.interval_days, 1)

    def schedules(self) -> dict[tuple[int, str], list[SourceConfig]]:
        """Aktywne źródła pogrupowane po slocie (dzień cyklu, godzina)."""
        groups: dict[tuple[int, str], list[SourceConfig]] = {}
        for source in self.sources:
            if source.enabled:
                groups.setdefault(source.slot(self.interval_days, self.run_at), []).append(source)
        return dict(sorted(groups.items()))

    def hours(self) -> list[str]:
        """Godziny, o których cokolwiek się dzieje — po jednym zadaniu cron."""
        return sorted({at for _, at in self.schedules()})


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} — oczekiwano liczby całkowitej") from None
    if value <= 0:
        raise ConfigError(f"{name}={raw!r} — oczekiwano liczby dodatniej")
    return value


def _env_minutes(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    """Lista odstępów ponawiania, np. "15,30,60". Pusta wartość wyłącza ponawianie."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    try:
        minutes = tuple(int(part) for part in parts)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} — oczekiwano liczb minut po przecinku") from None
    if any(value <= 0 for value in minutes):
        raise ConfigError(f"{name}={raw!r} — odstępy muszą być dodatnie")
    return minutes


def load_sources(path: Path) -> tuple[SourceConfig, ...]:
    if not path.is_file():
        raise ConfigError(f"nie znaleziono pliku konfiguracji: {path}")

    with path.open("rb") as handle:
        data = tomllib.load(handle)

    blocks = data.get("source")
    if not blocks:
        raise ConfigError(f"{path}: brak choćby jednego bloku [[source]]")

    sources: list[SourceConfig] = []
    seen_names: set[str] = set()
    seen_outputs: set[str] = set()

    for index, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            raise ConfigError(f"{path}: blok [[source]] #{index} nie jest tabelą")

        options = dict(block)
        try:
            name = str(options.pop("name"))
            output = str(options.pop("output"))
            resolver = str(options.pop("resolver"))
            select = str(options.pop("select"))
        except KeyError as exc:
            raise ConfigError(
                f"{path}: blok [[source]] #{index} nie ma wymaganego pola {exc.args[0]!r}"
            ) from exc

        if select not in VALID_POLICIES:
            raise ConfigError(
                f"{path}: źródło {name!r} ma nieznaną politykę select={select!r}; "
                f"dozwolone: {', '.join(sorted(VALID_POLICIES))}"
            )
        if name in seen_names:
            raise ConfigError(f"{path}: zduplikowana nazwa źródła {name!r}")
        # Dwa źródła pisujące do tego samego pliku wygrywałyby ze sobą przy każdym przebiegu.
        if output in seen_outputs:
            raise ConfigError(f"{path}: dwa źródła zapisują do tego samego pliku {output!r}")
        if "/" in output or output in {".", ".."}:
            raise ConfigError(f"{path}: output {output!r} musi być samą nazwą pliku")

        run_at = str(options.pop("run_at", "")).strip()
        if run_at and not _RUN_AT.match(run_at):
            raise ConfigError(
                f"{path}: źródło {name!r} ma run_at={run_at!r} — oczekiwano godziny HH:MM"
            )
        try:
            run_day = int(options.pop("run_day", 0))
        except (TypeError, ValueError):
            raise ConfigError(f"{path}: źródło {name!r} ma run_day spoza liczb całkowitych") from None
        if run_day < 0:
            raise ConfigError(f"{path}: źródło {name!r} ma run_day={run_day} — oczekiwano 0 lub więcej")

        seen_names.add(name)
        seen_outputs.add(output)
        sources.append(
            SourceConfig(
                name=name,
                output=output,
                resolver=resolver,
                select=select,
                enabled=bool(options.pop("enabled", True)),
                transform=str(options.pop("transform", "")),
                shape=str(options.pop("shape", "")).strip(),
                shape_area=str(options.pop("shape_area", "")).strip(),
                run_at=run_at,
                run_day=run_day,
                options=options,
            )
        )

    return tuple(sources)


def load_config() -> AppConfig:
    root = Path(__file__).resolve().parent.parent
    sources_path = Path(os.environ.get("FEEDSODT_SOURCES", root / "config" / "sources.toml"))
    output_dir = Path(os.environ.get("FEEDSODT_OUTPUT_DIR", root / "gtfs"))

    run_at = os.environ.get("FEEDSODT_RUN_AT", "00:30")
    if not _RUN_AT.match(run_at):
        raise ConfigError(f"FEEDSODT_RUN_AT={run_at!r} — oczekiwano godziny HH:MM")

    return AppConfig(
        sources=load_sources(sources_path),
        output_dir=output_dir,
        # Domyślnie wewnątrz katalogu wyjściowego, bo os.replace jest atomowe tylko
        # w obrębie jednego systemu plików. Kropka na początku ukrywa katalog
        # przed listingiem i przed nginx-em.
        work_dir=Path(os.environ.get("FEEDSODT_WORK_DIR", output_dir / ".work")),
        cache_dir=Path(os.environ.get("FEEDSODT_CACHE_DIR", output_dir / ".cache")),
        config_dir=sources_path.parent,
        run_at=run_at,
        interval_days=_env_int("FEEDSODT_INTERVAL_DAYS", 3),
        run_on_start=_env_bool("FEEDSODT_RUN_ON_START", True),
        timezone=os.environ.get("TZ", "Europe/Warsaw"),
        rail_url=os.environ.get("FEEDSODT_RAIL_URL", "http://railrouting:8989"),
        bus_url=os.environ.get("FEEDSODT_BUS_URL", "http://graphhopper-bus:8989"),
        shape_workers=_env_int("FEEDSODT_SHAPE_WORKERS", 8),
        shape_gps_accuracy=_env_int("FEEDSODT_SHAPE_GPS_ACCURACY", 50),
        retry_minutes=_env_minutes("FEEDSODT_RETRY_MINUTES", (15, 30, 60)),
    )
