"""Orkiestracja: od konfiguracji źródła do pliku pod stałą nazwą.

Ta sama ścieżka obsługuje wszystkie miasta — różnią się tylko resolverem
i polityką wyboru. Cała powtarzalna robota (HTTP, walidacja, sha256, atomowa
podmiana, index.json) jest tutaj i nie powiela się przy dodawaniu kolejnego
źródła.
"""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from . import gtfs
from .area import Area, AreaError
from .config import (
    AppConfig,
    Candidate,
    ConfigError,
    FeedWindow,
    Package,
    SourceConfig,
    load_config,
)
from .http import DownloadError, build_session, download_to, sha256_file
from .merge import MergeError, merge
from .publish import Index, publish
from .shaping import ShapeConfig, ShapeError, parse_modes
from .shaping import shape as make_shapes
from .sources import ResolveError, get_resolver
from .transform import Context, TransformError, get_transform

log = logging.getLogger(__name__)

#: Bezpiecznik na pobieranie w polityce today_forward. GZM ma 9 paczek,
#: Wrocław po odsianiu archiwum zwykle 3. Limit chroni przed sytuacją, w której
#: źródło nagle wystawi setki plików bez dat w nazwach.
MAX_DOWNLOADS = 15


@dataclass
class SourceResult:
    name: str
    status: str  # "updated" | "unchanged" | "failed"

    @property
    def ok(self) -> bool:
        return self.status != "failed"


def _now() -> datetime:
    return datetime.now().astimezone()


def _fetch(session: requests.Session, candidate: Candidate, target: Path) -> Package:
    """Pobiera kandydata, sprawdza go i zwraca gotową paczkę."""
    info = download_to(session, candidate.url, target, candidate.filename, auth=candidate.auth)
    # Resolver zna tylko adres, a ten bywa nieinformatywny (/hdb/download/127/).
    # Prawdziwą nazwę podaje dopiero serwer w Content-Disposition.
    if info.filename != candidate.filename:
        candidate = replace(candidate, filename=info.filename)
    return Package(candidate=candidate, window=gtfs.validate(target), path=target)


def _select(
    cfg: SourceConfig,
    session: requests.Session,
    candidates: list[Candidate],
    work: Path,
    today: date,
) -> tuple[list[Package], Path]:
    """Pobiera to, co trzeba, i zwraca wybrane paczki wraz z gotowym plikiem."""
    if cfg.select == "single":
        if len(candidates) > 1:
            log.warning(
                "[%s] wzorzec pasuje do %d plików, biorę pierwszy (%s) — "
                "doprecyzuj link_pattern/link_text, jeśli to nie ten",
                cfg.name,
                len(candidates),
                candidates[0].filename,
            )
        package = _fetch(session, candidates[0], work / "feed.zip")
        return [package], package.path

    # today_forward
    shortlisted = gtfs.shortlist(candidates, today, MAX_DOWNLOADS)
    probed: list[Package] = []

    for index, candidate in enumerate(shortlisted):
        try:
            package = _fetch(session, candidate, work / f"part{index:02d}.zip")
        except (DownloadError, gtfs.GtfsError) as exc:
            # Jedna wadliwa paczka nie może wywrócić całego łańcucha.
            log.warning("[%s] pomijam %s: %s", cfg.name, candidate.filename, exc)
            continue
        probed.append(package)
        log.info("[%s] %s -> %s", cfg.name, package.filename, package.window)

    chain = gtfs.chain_from_today(probed, today)
    if not chain:
        raise gtfs.GtfsError(
            f"żadna z {len(probed)} sprawdzonych paczek nie obejmuje {today:%Y-%m-%d}"
        )

    if len(chain) == 1:
        # Jedna paczka — publikujemy ją bez ruszania, bajt w bajt jak u źródła.
        return chain, chain[0].path

    staged = work / "merged.zip"
    merge(chain, staged)
    gtfs.validate(staged)
    return chain, staged


def _apply_transform(
    cfg: SourceConfig, staged: Path, work: Path, app: AppConfig, session: requests.Session
) -> Path:
    """Przebudowuje pobrany feed. Wynik ląduje obok, więc przy błędzie
    oryginał zostaje nietknięty i można go obejrzeć w logach.

    `transform` może wymieniać kilka funkcji po przecinku — wtedy feed
    przechodzi przez nie po kolei. Dzięki temu naprawa modelu konkretnego
    przewoźnika (`ska`) nie musi powtarzać tego, co robi wspólne kolorowanie
    i podmiana wystawcy (`odt`).
    """
    context = Context(
        session=session,
        cache_dir=app.cache_dir,
        config_dir=app.config_dir,
        options=cfg.options,
    )
    steps = [name.strip() for name in cfg.transform.split(",") if name.strip()]
    for index, name in enumerate(steps):
        rebuilt = work / f"transformed{index}.zip"
        log.info("[%s] przebudowa feedu: %s", cfg.name, name)
        get_transform(name)(staged, rebuilt, context)
        staged = rebuilt
    return staged


def _apply_shape(cfg: SourceConfig, staged: Path, work: Path, app: AppConfig) -> tuple[Path, dict]:
    """Dorabia shapes.txt routerem po OSM. Zwraca plik i podsumowanie do index.json.

    Silniki stoją w tle jako osobne kontenery, a geometria jest cache'owana
    między przebiegami, więc typowa doba to kilka nowych kształtów, a nie
    komplet od zera.
    """
    shaped = work / "shaped.zip"
    log.info("[%s] dorabiam geometrię: %s", cfg.name, cfg.shape)
    stats = make_shapes(
        staged,
        shaped,
        ShapeConfig(
            modes=parse_modes(cfg.shape),
            rail_url=app.rail_url,
            bus_url=app.bus_url,
            workers=app.shape_workers,
            gps_accuracy=app.shape_gps_accuracy,
            area=Area.load(cfg.shape_area) if cfg.shape_area else None,
            cache_path=app.cache_dir / f"shapes-{cfg.name}.sqlite",
        ),
    )
    summary = {
        "modes": sorted(parse_modes(cfg.shape)),
        "area": cfg.shape_area or None,
        "shapes": stats.shaped,
        "sequences": stats.sequences,
        "from_cache": stats.from_cache,
        "unroutable": stats.known_bad + stats.methods.get("skipped", 0),
        "trips_outside_area": stats.trips_outside_area,
        "rejected": stats.rejected,
        "trips_with_shape": stats.trips_shaped,
        "trips_shapeable": stats.shapeable,
        "coverage": round(stats.coverage, 4),
    }
    return shaped, summary


def run_source(
    cfg: SourceConfig, session: requests.Session, app: AppConfig, index: Index
) -> SourceResult:
    today = date.today()
    now = _now()
    work = app.work_dir / cfg.name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    previous = index.entry(cfg.name)
    output_path = app.output_dir / cfg.output

    try:
        candidates = get_resolver(cfg.resolver)(cfg, session)

        # Pobieramy zawsze, bez skrótów na metadanych listy. Powód: część źródeł
        # podmienia plik pod niezmiennym adresem i nie podaje ani rozmiaru, ani
        # daty (Gorzów: /files/file_add/download/77_gtfsgw.zip). Każdy odcisk
        # liczony z samej listy byłby tam wieczne "bez zmian". O tym, czy jest co
        # publikować, decyduje dopiero sha256 pobranej treści.
        chain, staged = _select(cfg, session, candidates, work, today)
        window = FeedWindow(
            start=min(package.window.start for package in chain),
            end=max(package.window.end for package in chain),
        )

        shapes: dict | None = None
        if cfg.transform:
            staged = _apply_transform(cfg, staged, work, app, session)
            # Przebudowa może zmienić okno (np. dopisać brakujące feed_info.txt),
            # więc odczytujemy je z gotowego pliku, a nie z pobranych paczek.
            window = gtfs.validate(staged)
        if cfg.shape:
            # Kształtowanie idzie po przebudowie: geometria ma być liczona
            # z poprawionych współrzędnych peronów, nie z punktów stacji.
            staged, shapes = _apply_shape(cfg, staged, work, app)
            gtfs.validate(staged)
        digest = sha256_file(staged)
        size = staged.stat().st_size
        ahead = (window.end - today).days

        if not window.covers(today):
            log.warning(
                "[%s] UWAGA: publikowany feed (%s) nie obejmuje dzisiejszej daty",
                cfg.name,
                window,
            )
        elif ahead <= app.interval_days:
            # Próg wynika z cyklu, a nie ze stałej: przy odpytywaniu co 3 dni
            # feed kończący się za 3 dni wygaśnie, zanim zdążymy zajrzeć po
            # następną wersję.
            log.warning(
                "[%s] UWAGA: feed kończy się za %d dni (%s), a następne sprawdzenie "
                "jest za %d — źródło może nie nadążać",
                cfg.name,
                ahead,
                window.end,
                app.interval_days,
            )

        entry = {
            "output": cfg.output,
            "policy": cfg.select,
            "resolver": cfg.resolver,
            "feed_start": f"{window.start:%Y-%m-%d}",
            "feed_end": f"{window.end:%Y-%m-%d}",
            "covers_today": window.covers(today),
            "days_ahead": ahead,
            "sha256": digest,
            "size": size,
            "sources": [
                {"filename": p.filename, "window": str(p.window), "url": p.url} for p in chain
            ],
            "checked_at": now.isoformat(timespec="seconds"),
        }
        if shapes is not None:
            entry["shapes"] = shapes

        if previous.get("sha256") == digest and output_path.is_file():
            entry["fetched_at"] = previous.get("fetched_at", now.isoformat(timespec="seconds"))
            index.put(cfg.name, entry)
            log.info("[%s] treść identyczna z opublikowaną, zostawiam %s", cfg.name, cfg.output)
            return SourceResult(cfg.name, "unchanged")

        publish(staged, app.output_dir, cfg.output)
        entry["fetched_at"] = now.isoformat(timespec="seconds")
        index.put(cfg.name, entry)
        log.info(
            "[%s] opublikowano %s (%.1f MB, %s, +%d dni)",
            cfg.name,
            cfg.output,
            size / 1e6,
            window,
            ahead,
        )
        return SourceResult(cfg.name, "updated")

    except (
        ResolveError,
        DownloadError,
        gtfs.GtfsError,
        MergeError,
        TransformError,
        ShapeError,
        AreaError,
        OSError,
    ) as exc:
        log.error("[%s] przebieg nieudany: %s", cfg.name, exc)
        if output_path.is_file():
            log.error("[%s] poprzednia wersja %s pozostaje opublikowana", cfg.name, cfg.output)
        return SourceResult(cfg.name, "failed")
    finally:
        shutil.rmtree(work, ignore_errors=True)


class AlreadyRunning(Exception):
    """Inny przebieg trzyma blokadę — ten się nie odbędzie."""


@contextmanager
def _exclusive(work_dir: Path):
    """Blokada plikowa na czas przebiegu.

    Musi być plikowa, nie wewnątrzprocesowa: `docker compose exec ... run-once`
    to osobny proces niż demon. Dwa przebiegi naraz kasowałyby sobie nawzajem
    katalogi robocze (każde źródło czyści swój na starcie) i nadpisywały index.json.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    handle = os.open(work_dir / ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AlreadyRunning("inny przebieg jest w toku") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def select_sources(
    app: AppConfig,
    only: set[str] | None = None,
    at: str | None = None,
    today: date | None = None,
) -> list[SourceConfig]:
    """Źródła do przebiegu: wskazane po nazwie (ponowienie), przypadające na
    dany slot (przebieg z harmonogramu) albo wszystkie (`run-once` z ręki).

    Slot to para (dzień cyklu, godzina). Przy FEEDSODT_INTERVAL_DAYS=3 każde
    źródło ma własny slot w trzydniowym obrocie, więc na maszynie nigdy nie
    liczą się dwa naraz.

    Slot wyznaczamy z aktualnie wczytanej konfiguracji, a nie z listy
    zapamiętanej przy starcie — dzięki temu dopisanie miasta do sources.toml
    wchodzi do właściwego slotu bez restartu kontenera.
    """
    active = [cfg for cfg in app.sources if cfg.enabled]
    if only is not None:
        return [cfg for cfg in active if cfg.name in only]
    if at is not None:
        day = app.cycle_day(today or date.today())
        return [cfg for cfg in active if cfg.slot(app.interval_days, app.run_at) == (day, at)]
    return active


def run_all(
    app: AppConfig,
    only: set[str] | None = None,
    at: str | None = None,
    today: date | None = None,
) -> list[SourceResult]:
    """Przebieg po źródłach: wszystkich, z jednej grupy godzinowej albo — przy
    ponawianiu — tylko po wskazanych.

    Rzuca AlreadyRunning, gdy inny proces właśnie pobiera.
    """
    app.output_dir.mkdir(parents=True, exist_ok=True)

    with _exclusive(app.work_dir):
        index = Index.load(app.output_dir)
        session = build_session()
        results: list[SourceResult] = []

        active = select_sources(app, only, at, today)
        if only is not None:
            # Ta sama ścieżka obsługuje ponowienie po błędzie i ręczne
            # `run-once --only`, więc komunikat mówi o wyborze, nie o przyczynie;
            # o ponowieniu informuje osobny wpis przy planowaniu.
            log.info(
                "przebieg po wskazanych źródłach: %s",
                ", ".join(cfg.name for cfg in active) or "brak pasujących",
            )
        elif at is not None:
            if not active:
                log.info("slot %s w dniu %d cyklu jest pusty — nic do zrobienia",
                         at, app.cycle_day(today or date.today()))
            else:
                log.info(
                    "start przebiegu %s (dzień %d/%d cyklu): %s -> %s",
                    at,
                    app.cycle_day(today or date.today()) + 1,
                    app.interval_days,
                    ", ".join(cfg.name for cfg in active),
                    app.output_dir,
                )
        else:
            log.info("start przebiegu: %d źródeł -> %s", len(active), app.output_dir)

        for cfg in active:
            results.append(run_source(cfg, session, app, index))

        index.save(_now())
        session.close()

    summary = ", ".join(f"{r.name}={r.status}" for r in results)
    if any(not result.ok for result in results):
        log.error("koniec przebiegu: %s", summary)
    else:
        log.info("koniec przebiegu: %s", summary)
    return results


def _scheduled_run(
    fallback: AppConfig,
    scheduler,
    at: str | None = None,
    only: set[str] | None = None,
    attempt: int = 0,
) -> None:
    """Jeden przebieg plus zaplanowanie ponowienia, jeśli coś zawiodło.

    Konfigurację wczytujemy od nowa, żeby dodanie miasta w sources.toml nie
    wymagało restartu kontenera. Literówka w TOML-u nie może zablokować
    pobierania — wtedy wracamy do konfiguracji ze startu.
    """
    try:
        app = load_config()
    except ConfigError as exc:
        log.error("sources.toml niepoprawny (%s) — używam konfiguracji ze startu", exc)
        app = fallback

    try:
        results = run_all(app, only, at)
    except AlreadyRunning:
        # Ktoś odpalił run-once ręcznie i właśnie pobiera to samo — nie ma sensu
        # ani dublować pracy, ani ponawiać.
        log.warning("inny przebieg jest w toku — pomijam to uruchomienie")
        return

    failed = {result.name for result in results if not result.ok}
    if not failed:
        return

    if attempt >= len(app.retry_minutes):
        log.error(
            "wyczerpano ponowienia dla: %s — następna próba w kolejnym slocie tych źródeł",
            ", ".join(sorted(failed)),
        )
        return

    delay = app.retry_minutes[attempt]
    when = _now() + timedelta(minutes=delay)
    scheduler.add_job(
        _scheduled_run,
        DateTrigger(run_date=when),
        # Ponowienie idzie po nazwach, nie po grupie: interesują nas dokładnie
        # te źródła, które zawiodły, niezależnie od tego, o której się liczą.
        args=[fallback, scheduler, None, failed, attempt + 1],
        id=f"retry-{at or 'manual'}-{attempt}",
        replace_existing=True,
        misfire_grace_time=600,
    )
    log.warning(
        "ponowienie %d/%d za %d min (o %s) dla: %s",
        attempt + 1,
        len(app.retry_minutes),
        delay,
        when.strftime("%H:%M"),
        ", ".join(sorted(failed)),
    )


def run_daemon(app: AppConfig) -> None:
    """Uruchamia przebiegi z harmonogramu i nie kończy się — kontener ma po
    prostu żyć.

    Zadanie cron zakładamy raz na każdą **godzinę** występującą w konfiguracji,
    a o tym, które źródła się w niej mieszczą, decyduje dopiero dzień cyklu
    w chwili odpalenia. Dzięki temu trzydniowy obrót nie potrzebuje wyrażeń
    typu ``day=*/3``, które rozjeżdżają się na przełomie miesiąca (31 -> 1 daje
    odstęp jednego dnia zamiast trzech).

    Efekt: każde źródło ma własny slot, więc na maszynie nigdy nie liczą się
    dwa naraz.
    """
    scheduler = BlockingScheduler(timezone=app.timezone)

    for at in app.hours():
        hour, minute = (int(part) for part in at.split(":", 1))
        scheduler.add_job(
            _scheduled_run,
            CronTrigger(hour=hour, minute=minute, timezone=app.timezone),
            args=[app, scheduler, at],
            id=f"slot-{at.replace(':', '')}",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )

    log.info(
        "harmonogram: cykl %d-dniowy, %s (%s)",
        app.interval_days,
        f"{len(app.hours())} godzin w dobie" if app.hours() else "brak slotów",
        app.timezone,
    )
    for (day, at), sources in app.schedules().items():
        log.info(
            "  dzień %d/%d o %s — %s",
            day + 1,
            app.interval_days,
            at,
            ", ".join(source.name for source in sources),
        )

    # Przebieg startowy bierze wszystkie źródła naraz, żeby `up -d` dawało
    # komplet plików bez czekania trzech dni. Idzie tą samą drogą co
    # harmonogramowy, więc też dostaje ponowienia.
    if app.run_on_start:
        scheduler.add_job(
            _scheduled_run,
            DateTrigger(run_date=_now() + timedelta(seconds=1)),
            args=[app, scheduler],
            id="startup",
        )

    log.info(
        "ponowienia po %s min",
        ", ".join(str(minutes) for minutes in app.retry_minutes) or "brak",
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("zatrzymano")
