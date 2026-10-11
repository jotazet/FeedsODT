"""Okno pracy silników z shaper/engines.crontab kontra harmonogram źródeł.

Silniki są włączane przez crona tylko na noc. Źródło z polem `shape`, którego
slot (albo któreś z ponowień) wypada poza tym oknem, zawodziłoby co cykl —
po cichu, bo poprzednia wersja feedu zostaje opublikowana.
"""

from __future__ import annotations

import re
from pathlib import Path

from feedsodt.config import AppConfig, load_sources

ROOT = Path(__file__).resolve().parent.parent
CRONTAB = ROOT / "shaper" / "engines.crontab"
SOURCES = ROOT / "config" / "sources.toml"

#: Zapas na sam silnik (start trwa kilka sekund) i na czas trwania przebiegu
#: przed każdym ponowieniem — pierwsze liczenie geometrii polregio to ~3,5 min.
START_MARGIN_MIN = 5
RUN_MARGIN_MIN = 5

_LINE = re.compile(r"^(\d+) (\d+) \* \* \* \S*engines\.sh (start|stop)\b")


def _window() -> dict[str, int]:
    times = {}
    for line in CRONTAB.read_text(encoding="utf-8").splitlines():
        match = _LINE.match(line)
        if match:
            minute, hour, action = match.groups()
            times[action] = int(hour) * 60 + int(minute)
    return times


def _minutes(at: str) -> int:
    hour, minute = at.split(":")
    return int(hour) * 60 + int(minute)


def test_crontab_ma_start_i_stop_w_strefie_harmonogramu():
    text = CRONTAB.read_text(encoding="utf-8")
    # Harmonogram fetchera liczy się w Europe/Warsaw (TZ w docker-compose).
    # Bez CRON_TZ cron brałby strefę hosta — zwykle UTC, czyli o 1–2 h obok.
    assert "CRON_TZ=Europe/Warsaw" in text
    assert set(_window()) == {"start", "stop"}


def test_okno_obejmuje_wszystkie_sloty_z_shape_i_ponowienia():
    window = _window()
    retries = AppConfig.__dataclass_fields__["retry_minutes"].default
    default_run_at = "00:30"  # FEEDSODT_RUN_AT w docker-compose

    shaped = [s for s in load_sources(SOURCES) if s.enabled and s.shape]
    assert shaped, "w sources.toml nie ma źródeł z `shape` — po co okno?"

    for source in shaped:
        start = _minutes(source.schedule(default_run_at))
        # Ponowienia liczą się od końca poprzedniej próby, nie od slotu.
        last_retry_ends = start + sum(retries) + RUN_MARGIN_MIN * (len(retries) + 1)
        assert start - START_MARGIN_MIN >= window["start"], (
            f"{source.name}: slot {source.schedule(default_run_at)} jest przed włączeniem silników"
        )
        assert last_retry_ends <= window["stop"], (
            f"{source.name}: ostatnie ponowienie kończy się ok. "
            f"{last_retry_ends // 60:02d}:{last_retry_ends % 60:02d}, po wyłączeniu silników"
        )
