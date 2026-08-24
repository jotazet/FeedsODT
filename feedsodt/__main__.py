"""Punkt wejścia: `python -m feedsodt <komenda>`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from .config import ConfigError, load_config
from .runner import AlreadyRunning, run_all, run_daemon
from .sources import get_resolver


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,  # w kontenerze wszystko ma iść na stdout
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="feedsodt",
        description="Pobiera najnowsze GTFS i wystawia je pod stałymi nazwami plików.",
    )
    parser.add_argument(
        "command",
        choices=("run-once", "daemon", "list"),
        help="run-once: jeden przebieg po wszystkich źródłach; daemon: harmonogram; "
        "list: pokaż skonfigurowane źródła",
    )
    parser.add_argument(
        "--only",
        default="",
        metavar="NAZWY",
        help="run-once: przebieg tylko po wskazanych źródłach (po przecinku). "
        "Przydaje się przy dokładaniu miasta i przy źródłach, które liczą się "
        "minutami, jak polregio z kształtowaniem geometrii.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="logi debugowania")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    try:
        app = load_config()
    except ConfigError as exc:
        print(f"błąd konfiguracji: {exc}", file=sys.stderr)
        return 2

    if args.command == "list":
        print(f"katalog wyjściowy: {app.output_dir}")
        retries = ", ".join(f"+{minutes} min" for minutes in app.retry_minutes)
        print(f"ponowienia przy błędzie: {retries or 'wyłączone'}")
        print(f"strefa czasowa: {app.timezone}")
        print(f"cykl: co {app.interval_days} dni")
        dzis = app.cycle_day(date.today())
        for (day, at), group in app.schedules().items():
            teraz = "  <- dzisiaj" if day == dzis else ""
            print(f"\ndzień {day + 1}/{app.interval_days} o {at}:{teraz}")
            for cfg in group:
                kroki = [cfg.resolver, cfg.select]
                if cfg.transform:
                    kroki.append(f"transform={cfg.transform}")
                if cfg.shape:
                    kroki.append(f"shape={cfg.shape}")
                print(f"  {cfg.name:14} -> {cfg.output:26} {' '.join(kroki)}")
                try:
                    get_resolver(cfg.resolver)
                except ConfigError as exc:
                    print(f"  {'':14}    UWAGA: {exc}")
        wylaczone = [cfg.name for cfg in app.sources if not cfg.enabled]
        if wylaczone:
            print(f"\nwyłączone: {', '.join(wylaczone)}")
        return 0

    if args.command == "daemon":
        run_daemon(app)
        return 0

    only = {name.strip() for name in args.only.split(",") if name.strip()} or None
    if only:
        known = {cfg.name for cfg in app.sources}
        unknown = only - known
        if unknown:
            print(
                f"nieznane źródła: {', '.join(sorted(unknown))}; "
                f"dostępne: {', '.join(sorted(known))}",
                file=sys.stderr,
            )
            return 2

    try:
        results = run_all(app, only)
    except AlreadyRunning:
        print(
            "inny przebieg właśnie trwa (demon albo drugie run-once) — nic nie robię",
            file=sys.stderr,
        )
        return 3

    # Kod wyjścia mówi o powodzeniu przebiegu — przydaje się w cronie i w healthchecku.
    return 1 if any(not result.ok for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
