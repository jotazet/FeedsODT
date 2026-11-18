"""Uruchomienie z harmonogramu, które trafiło na trwający przebieg.

Sloty stoją co 10 min, więc ponowienie, przebieg startowy albo ręczny run-once
potrafią jeszcze trwać, gdy wypada kolejny slot. Pominięcie takiego slotu
znaczyłoby, że źródło czeka na nową wersję cały dzień.
"""

from __future__ import annotations

from feedsodt import runner


class Scheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger, args, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, "args": args, **kwargs})


def _busy(*_args, **_kwargs):
    raise runner.AlreadyRunning("inny przebieg jest w toku")


def test_slot_na_zajetej_blokadzie_czeka_zamiast_przepasc(monkeypatch):
    monkeypatch.setattr(runner, "load_config", lambda: object())
    monkeypatch.setattr(runner, "run_all", _busy)
    scheduler = Scheduler()

    runner._scheduled_run(object(), scheduler, at="00:50")

    [job] = scheduler.jobs
    assert job["func"] is runner._scheduled_run
    # Ten sam slot, to samo ponowienie, jedno oczekiwanie więcej.
    assert job["args"][2:] == ["00:50", None, 0, 1]


def test_po_wyczerpaniu_oczekiwan_slot_jest_pomijany(monkeypatch):
    monkeypatch.setattr(runner, "load_config", lambda: object())
    monkeypatch.setattr(runner, "run_all", _busy)
    scheduler = Scheduler()

    runner._scheduled_run(object(), scheduler, at="00:50", waits=runner.LOCK_MAX_WAITS)

    assert scheduler.jobs == []


def test_ponowienia_roznych_zrodel_nie_nadpisuja_sie(monkeypatch):
    """Dwa ponowienia z tym samym numerem próby (at=None) muszą mieć różne id:
    scheduler zakłada zadania z replace_existing=True."""

    class App:
        retry_minutes = [15, 30, 60]

    def failed(_app, only, _at):
        return [runner.SourceResult(name, "failed") for name in only]

    monkeypatch.setattr(runner, "load_config", lambda: App())
    monkeypatch.setattr(runner, "run_all", failed)
    scheduler = Scheduler()

    runner._scheduled_run(object(), scheduler, only={"wroclaw"}, attempt=1)
    runner._scheduled_run(object(), scheduler, only={"gzm-extend"}, attempt=1)
    monkeypatch.setattr(runner, "run_all", _busy)
    runner._scheduled_run(object(), scheduler, only={"wroclaw"}, attempt=1)
    runner._scheduled_run(object(), scheduler, only={"gzm-extend"}, attempt=1)

    ids = [job["id"] for job in scheduler.jobs]
    assert len(set(ids)) == 4, ids
