"""Testy scalarki.

Każdy przypadek odtwarza konkretną właściwość prawdziwych paczek GZM:
kolidujące service_id i trip_id, powtórzone przystanki i kształty, oraz warianty
tras różniące się między oknami.
"""

from __future__ import annotations

from datetime import date

import pytest

from conftest import feed_tables, read_gtfs, write_gtfs
from feedsodt.config import Candidate, FeedWindow, Package
from feedsodt.merge import MergeError, merge


def item(path, start: str, end: str) -> Package:
    return Package(
        candidate=Candidate(url=f"https://example.pl/{path.name}", filename=path.name),
        window=FeedWindow(date.fromisoformat(start), date.fromisoformat(end)),
        path=path,
    )


@pytest.fixture
def kolidujace(tmp_path):
    """Dwie paczki z **tym samym** service_id i trip_id, ale innymi datami —
    dokładnie tak wygląda to u GZM (service_id „8" w kilku oknach)."""
    a = write_gtfs(
        tmp_path / "schedule_ZTM_2026.08.21_10909_0005.zip",
        feed_tables(start=date(2026, 8, 21), end=date(2026, 8, 21), service_id="8", trip_ids=("8_1", "8_2")),
    )
    b = write_gtfs(
        tmp_path / "schedule_ZTM_2026.08.21_10910_0038.zip",
        feed_tables(start=date(2026, 8, 22), end=date(2026, 8, 22), service_id="8", trip_ids=("8_1", "8_2")),
    )
    return [item(a, "2026-08-21", "2026-08-21"), item(b, "2026-08-22", "2026-08-22")]


class TestIntegralnosc:
    def test_brak_duplikatow_trip_id(self, kolidujace, tmp_path):
        """Bez prefiksowania obie paczki wniosłyby trip_id „8_1" i plik byłby
        niepoprawnym GTFS-em."""
        out = tmp_path / "merged.zip"
        merge(kolidujace, out)

        trips = [r["trip_id"] for r in read_gtfs(out, "trips.txt")]

        assert len(trips) == 4
        assert len(set(trips)) == 4

    def test_stop_times_wskazuje_na_istniejace_kursy(self, kolidujace, tmp_path):
        """Prefiks musi trafić do stop_times.txt, nie tylko do trips.txt."""
        out = tmp_path / "merged.zip"
        merge(kolidujace, out)

        trip_ids = {r["trip_id"] for r in read_gtfs(out, "trips.txt")}
        uzyte = {r["trip_id"] for r in read_gtfs(out, "stop_times.txt")}

        assert uzyte <= trip_ids
        assert uzyte == trip_ids

    def test_kazdy_kurs_ma_swoj_kalendarz(self, kolidujace, tmp_path):
        """service_id też dostaje prefiks — inaczej oba okna dzieliłyby jeden
        wpis kalendarza i połowa kursów jeździłaby w złe dni."""
        out = tmp_path / "merged.zip"
        merge(kolidujace, out)

        services = {r["service_id"] for r in read_gtfs(out, "calendar.txt")}
        uzyte = {r["service_id"] for r in read_gtfs(out, "trips.txt")}

        assert len(services) == 2
        assert uzyte == services

    def test_okno_wynikowe_obejmuje_wszystkie_paczki(self, kolidujace, tmp_path):
        out = tmp_path / "merged.zip"
        merge(kolidujace, out)

        info = read_gtfs(out, "feed_info.txt")[0]

        assert info["feed_start_date"] == "20260821"
        assert info["feed_end_date"] == "20260822"
        assert info["feed_version"].startswith("merged:")


class TestDeduplikacja:
    def test_przystanki_nie_sa_powielane(self, kolidujace, tmp_path):
        """7573 wspólne przystanki × 9 paczek to 68 tys. wierszy zamiast 7,6 tys."""
        out = tmp_path / "merged.zip"
        merge(kolidujace, out)

        stops = [r["stop_id"] for r in read_gtfs(out, "stops.txt")]

        assert stops == ["ST1", "ST2"]

    def test_ksztalty_nie_sa_powielane(self, kolidujace, tmp_path):
        """shapes.txt to największa tabela po stop_times — dedup po shape_id
        zbija 178 MB do ~25 MB."""
        out = tmp_path / "merged.zip"
        merge(kolidujace, out)

        punkty = read_gtfs(out, "shapes.txt")

        assert len(punkty) == 3
        assert {r["shape_id"] for r in punkty} == {"SH1"}


class TestGlosowanie:
    def test_wygrywa_wariant_wystepujacy_najczesciej(self, tmp_path):
        """Regresja na realny przypadek: 21.08 linia 5011 jechała skróconym
        wariantem z powodu objazdu. „Pierwszy wygrywa" przyklejał nazwę
        jednodniowej anomalii do całego feedu; głosowanie daje 2:1 na normalny."""
        paczki = [
            item(
                write_gtfs(
                    tmp_path / "p1.zip",
                    feed_tables(
                        start=date(2026, 8, 21),
                        end=date(2026, 8, 21),
                        service_id="S1",
                        trip_ids=("T1",),
                        route_long_name="ZAWODZIE - CHORZOW TEATR",  # objazd
                    ),
                ),
                "2026-08-21",
                "2026-08-21",
            ),
            item(
                write_gtfs(
                    tmp_path / "p2.zip",
                    feed_tables(
                        start=date(2026, 8, 22),
                        end=date(2026, 8, 22),
                        service_id="S2",
                        trip_ids=("T2",),
                        route_long_name="ZAWODZIE - CHEBZIE PETLA",
                    ),
                ),
                "2026-08-22",
                "2026-08-22",
            ),
            item(
                write_gtfs(
                    tmp_path / "p3.zip",
                    feed_tables(
                        start=date(2026, 8, 23),
                        end=date(2026, 8, 23),
                        service_id="S3",
                        trip_ids=("T3",),
                        route_long_name="ZAWODZIE - CHEBZIE PETLA",
                    ),
                ),
                "2026-08-23",
                "2026-08-23",
            ),
        ]
        out = tmp_path / "merged.zip"
        stats = merge(paczki, out)

        trasy = read_gtfs(out, "routes.txt")

        assert len(trasy) == 1
        assert trasy[0]["route_long_name"] == "ZAWODZIE - CHEBZIE PETLA"
        assert stats.conflicts["routes.txt"] == 1
        assert "route_long_name" in stats.conflict_fields["routes.txt"]


class TestOdpornosc:
    def test_determinizm_te_same_paczki_daja_ten_sam_plik(self, kolidujace, tmp_path):
        """Na tym opiera się wykrywanie zmian: gdyby scalanie stemplowało wpisy
        bieżącym czasem, mirror republikowałby 59 MB codziennie bez powodu."""
        pierwszy = tmp_path / "a.zip"
        drugi = tmp_path / "b.zip"
        merge(kolidujace, pierwszy)
        merge(kolidujace, drugi)

        assert pierwszy.read_bytes() == drugi.read_bytes()

    def test_niezgodne_naglowki_przerywaja_scalanie(self, tmp_path):
        """Cicha zmiana schematu u źródła jest gorsza niż nieudany przebieg:
        plik wyglądałby poprawnie, a miałby poprzesuwane kolumny."""
        zwykla = feed_tables(start=date(2026, 8, 21), end=date(2026, 8, 21), service_id="S1")
        inna = feed_tables(start=date(2026, 8, 22), end=date(2026, 8, 22), service_id="S2")
        for row in inna["stops.txt"]:
            row["stop_code"] = "999"  # nowa kolumna u źródła

        paczki = [
            item(write_gtfs(tmp_path / "a.zip", zwykla), "2026-08-21", "2026-08-21"),
            item(write_gtfs(tmp_path / "b.zip", inna), "2026-08-22", "2026-08-22"),
        ]

        with pytest.raises(MergeError, match="różne nagłówki"):
            merge(paczki, tmp_path / "merged.zip")

    def test_scalanie_jednej_paczki_jest_bledem(self, tmp_path):
        """Wołający ma opublikować taką paczkę bez ruszania, bajt w bajt."""
        jedna = [
            item(
                write_gtfs(
                    tmp_path / "a.zip",
                    feed_tables(start=date(2026, 8, 21), end=date(2026, 8, 30)),
                ),
                "2026-08-21",
                "2026-08-30",
            )
        ]

        with pytest.raises(MergeError):
            merge(jedna, tmp_path / "merged.zip")
