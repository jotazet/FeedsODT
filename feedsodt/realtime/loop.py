"""`python -m feedsodt realtime`: dwa pliki GTFS-RT odświeżane co zadaną liczbę sekund.

  <output>/rt/polish-trains-trip-updates.pb       TripUpdates z API PKP PLK (plk.py)
  <output>/rt/polish-trains-vehicle-positions.pb  VehiclePositions z feedów przewoźników (vehicles.py)

Osobny proces od demona feedów statycznych: przebieg statyczny trzyma blokadę
`.work/.lock` nawet kilkadziesiąt minut, a pliki RT mają się pojawiać co kilkadziesiąt
sekund. Pętle przeniesione z lokalnego prototypu (server.py) bez mapy i API.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from pathlib import Path

from ..config import AppConfig, load_realtime
from .plk import TUBuilder
from .vehicles import TZ, VPBuilder

log = logging.getLogger(__name__)

TU_FILE = "polish-trains-trip-updates.pb"
VP_FILE = "polish-trains-vehicle-positions.pb"
#: Starszy własny TU -> VehiclePositions bierze zapasowy national.rt z konfiguracji.
TU_MAX_AGE_S = 600
#: Najdłuższy odstęp po kolejnych błędach: 30 s * 2^6 = 32 min.
MAX_BACKOFF_STEPS = 6


class SharedTU:
    """Ostatni udany TripUpdates, z którego pętla VP liczy przewidywane pozycje."""

    def __init__(self):
        self.lock = threading.Lock()
        self.feed = None
        self.at = 0.0

    def put(self, feed):
        with self.lock:
            self.feed, self.at = feed, time.time()

    def fresh(self, now=None):
        with self.lock:
            if (now or time.time()) - self.at < TU_MAX_AGE_S:
                return self.feed
            return None


def next_tu_wait(info: dict, interval: int, now: dt.datetime) -> float:
    """Odstęp do następnego przebiegu TU. Gdy dobowy limit API nie starczy do północy
    przy obecnym tempie, rozkłada resztę zapytań równo do końca dnia."""
    rem = info["limits"].get("Daily-Remaining")
    if not isinstance(rem, int) or not info["requests"]:
        return interval
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
    left = (midnight - now).total_seconds()
    runs = rem / info["requests"]
    if runs < left / interval:
        wait = left / max(runs, 1)
        log.warning("TU: zostało %d zapytań na dziś -> odstęp wydłużony do %.0f s", rem, wait)
        return wait
    return interval


def failure_wait(ex: Exception, fails: int) -> tuple[int, int]:
    """(nowa liczba błędów z rzędu, odstęp w sekundach) po nieudanym przebiegu TU."""
    if "PendingActivation" in str(ex):  # klucz czeka na potwierdzenie maila: co 5 min
        return 0, 300
    fails = min(fails + 1, MAX_BACKOFF_STEPS)
    return fails, 30 * 2**fails


def tu_worker(builder: TUBuilder, out_path: Path, interval: int, shared: SharedTU) -> None:
    fails = 0
    while True:
        t0 = time.time()
        try:
            feed, info = builder.run_once(out_path)
            shared.put(feed)
            fails, wait = 0, next_tu_wait(info, interval, dt.datetime.now(TZ))
        except Exception as ex:
            fails, wait = failure_wait(ex, fails)
            log.error("błąd TU: %r; ponowna próba za %d s", ex, wait)
        time.sleep(max(1, wait - (time.time() - t0)))


def vp_worker(builder: VPBuilder, out_path: Path, interval: int, shared: SharedTU) -> None:
    while True:
        t0 = time.time()
        try:
            builder.run_once(out_path, rt_feed=shared.fresh())
        except Exception as ex:  # plik zostaje poprzedni, następny przebieg za chwilę
            log.error("błąd VP: %r", ex)
        time.sleep(max(1, interval - (time.time() - t0)))


def run_realtime(app: AppConfig) -> None:
    cfg = load_realtime(app.config_dir / "realtime.toml")
    out_dir = app.output_dir / "rt"
    cache_dir = app.cache_dir / "rt"
    out_dir.mkdir(parents=True, exist_ok=True)
    shared = SharedTU()

    apikey = os.environ.get("PKP_PLK_APIKEY", "").strip()
    if apikey:
        tu = TUBuilder(apikey, cfg["national"]["static"], cache_dir, cfg["static_max_age_hours"])
        threading.Thread(
            target=tu_worker,
            args=(tu, out_dir / TU_FILE, app.rt_tu_interval, shared),
            name="tu",
            daemon=True,
        ).start()
        log.info("TripUpdates co %d s -> %s", app.rt_tu_interval, out_dir / TU_FILE)
    else:
        log.warning(
            "brak PKP_PLK_APIKEY: TripUpdates nie powstaje, VehiclePositions bierze zapasowy %s",
            cfg["national"].get("rt") or "(brak)",
        )

    log.info("VehiclePositions co %d s -> %s", app.rt_vp_interval, out_dir / VP_FILE)
    vp_worker(VPBuilder(cfg, cache_dir), out_dir / VP_FILE, app.rt_vp_interval, shared)
