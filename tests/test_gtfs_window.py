"""Testy odczytu okna obowiązywania i walidacji paczki.

Okno bierzemy z wnętrza pliku, bo ani nazwa, ani metadane portalu nie są
wiarygodne. Ścieżka zapasowa (calendar.txt zamiast feed_info.txt) nie jest
teoretyczna — tak wygląda feed MKS Jasło.
"""

from __future__ import annotations

import zipfile
from datetime import date

import pytest

from conftest import feed_tables, write_gtfs
from feedsodt.config import FeedWindow
from feedsodt.gtfs import GtfsError, read_window, validate


class TestOknoObowiazywania:
    def test_bierze_daty_z_feed_info(self, build):
        path = build("a.zip", start=date(2026, 8, 17), end=date(2026, 9, 7))

        assert read_window(path) == FeedWindow(date(2026, 8, 17), date(2026, 9, 7))

    def test_bez_feed_info_liczy_z_kalendarza(self, tmp_path):
        """Jasło: feed_info.txt istnieje, ale nie ma w nim dat obowiązywania."""
        tables = feed_tables(start=date(2026, 3, 11), end=date(2027, 3, 12), with_feed_info=False)
        path = write_gtfs(tmp_path / "jaslo.zip", tables)

        assert read_window(path) == FeedWindow(date(2026, 3, 11), date(2027, 3, 12))

    def test_puste_feed_info_nie_przeslania_kalendarza(self, tmp_path):
        """Puste pola dat w feed_info.txt muszą przepuścić do kalendarza,
        a nie wyprodukować bezsensowne okno."""
        tables = feed_tables(start=date(2026, 5, 1), end=date(2026, 6, 1))
        tables["feed_info.txt"][0]["feed_start_date"] = ""
        tables["feed_info.txt"][0]["feed_end_date"] = ""
        path = write_gtfs(tmp_path / "puste.zip", tables)

        assert read_window(path) == FeedWindow(date(2026, 5, 1), date(2026, 6, 1))

    def test_bez_zadnych_dat_rzuca_bledem(self, tmp_path):
        tables = feed_tables(start=date(2026, 5, 1), end=date(2026, 6, 1), with_feed_info=False)
        tables["calendar.txt"] = []
        path = write_gtfs(tmp_path / "bez-dat.zip", tables)

        with pytest.raises(GtfsError, match="okresu obowiązywania"):
            read_window(path)


class TestWalidacja:
    def test_poprawna_paczka_przechodzi(self, build):
        path = build("ok.zip", start=date(2026, 8, 21), end=date(2026, 9, 10))

        assert validate(path).covers(date(2026, 8, 25))

    def test_odrzuca_plik_ktory_nie_jest_zipem(self, tmp_path):
        """Portale potrafią oddać stronę błędu z kodem 200."""
        path = tmp_path / "nie-zip.zip"
        path.write_text("<html>404 Not Found</html>", encoding="utf-8")

        with pytest.raises(GtfsError, match="nie jest archiwum ZIP"):
            validate(path)

    @pytest.mark.parametrize("brakujacy", ["stops.txt", "trips.txt", "stop_times.txt"])
    def test_odrzuca_paczke_bez_wymaganej_tabeli(self, tmp_path, brakujacy):
        tables = feed_tables(start=date(2026, 8, 21), end=date(2026, 9, 10))
        del tables[brakujacy]
        path = write_gtfs(tmp_path / "niepelna.zip", tables)

        with pytest.raises(GtfsError, match=brakujacy):
            validate(path)

    def test_odrzuca_paczke_bez_kalendarza(self, tmp_path):
        tables = feed_tables(start=date(2026, 8, 21), end=date(2026, 9, 10))
        tables["calendar.txt"] = []
        path = write_gtfs(tmp_path / "bez-kalendarza.zip", tables)

        with pytest.raises(GtfsError, match="calendar"):
            validate(path)

    def test_odrzuca_uszkodzone_dane_w_archiwum(self, build):
        """Regresja: rozsypany strumień deflate rzuca zlib.error, którego
        zipfile.testzip() u siebie NIE łapie (łapie tylko BadZipFile). Zanim
        validate zaczęło to przechwytywać, taki plik przerywał cały przebieg
        i pozostałe miasta nie były pobierane."""
        path = build("uszkodzony.zip", start=date(2026, 8, 21), end=date(2026, 9, 10))

        with zipfile.ZipFile(path) as archive:
            info = max(archive.infolist(), key=lambda i: i.compress_size)
            data_start = (
                info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
            )

        surowe = bytearray(path.read_bytes())
        for offset in range(data_start, data_start + 12):
            surowe[offset] ^= 0xFF
        path.write_bytes(bytes(surowe))

        with pytest.raises(GtfsError):
            validate(path)
