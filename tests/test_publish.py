"""Testy publikacji, index.json i blokady przebiegu.

To warstwa, w której błąd nie objawia się wyjątkiem, tylko uszkodzonym plikiem
u konsumenta — dlatego jest tu sprawdzana osobno.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from feedsodt.publish import Index, publish
from feedsodt.runner import AlreadyRunning, _exclusive


class TestPublikacja:
    def test_podmienia_plik_pod_stala_nazwa(self, tmp_path):
        output = tmp_path / "gtfs"
        output.mkdir()
        (output / "miasto-gtfs.zip").write_bytes(b"stara wersja")
        staged = output / ".work" / "nowa.zip"
        staged.parent.mkdir()
        staged.write_bytes(b"nowa wersja")

        dest = publish(staged, output, "miasto-gtfs.zip")

        assert dest.read_bytes() == b"nowa wersja"
        assert not staged.exists()

    def test_ustawia_prawa_do_odczytu(self, tmp_path):
        """nginx w osobnym kontenerze musi móc przeczytać plik."""
        output = tmp_path / "gtfs"
        staged = output / ".work" / "nowa.zip"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"x")
        staged.chmod(0o600)

        dest = publish(staged, output, "miasto-gtfs.zip")

        assert dest.stat().st_mode & 0o777 == 0o644


class TestIndex:
    def test_brak_pliku_to_pusty_index(self, tmp_path):
        assert Index.load(tmp_path).data == {"feeds": {}}

    def test_uszkodzony_index_nie_blokuje_publikacji(self, tmp_path):
        """Lepiej pobrać wszystko od nowa niż wywrócić przebieg przez metadane."""
        (tmp_path / "index.json").write_text("{to nie jest json", encoding="utf-8")

        assert Index.load(tmp_path).data == {"feeds": {}}

    def test_zapis_i_odczyt(self, tmp_path):
        index = Index.load(tmp_path)
        index.put("wroclaw", {"output": "wroclaw-gtfs.zip", "sha256": "abc"})
        index.save(datetime(2026, 8, 21, 12, 0))

        zapisany = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))

        assert zapisany["feeds"]["wroclaw"]["sha256"] == "abc"
        assert zapisany["generated_at"].startswith("2026-08-21")

    def test_zapis_jednego_zrodla_nie_kasuje_pozostalych(self, tmp_path):
        """Ponowienie przetwarza tylko nieudane źródła — reszta musi przetrwać."""
        pierwszy = Index.load(tmp_path)
        pierwszy.put("dziala", {"sha256": "aaa"})
        pierwszy.put("padlo", {"sha256": "bbb"})
        pierwszy.save(datetime(2026, 8, 21, 12, 0))

        ponowienie = Index.load(tmp_path)
        ponowienie.put("padlo", {"sha256": "ccc"})
        ponowienie.save(datetime(2026, 8, 21, 12, 15))

        feeds = Index.load(tmp_path).data["feeds"]

        assert feeds["dziala"]["sha256"] == "aaa"
        assert feeds["padlo"]["sha256"] == "ccc"

    def test_nieczytelny_wpis_zwraca_pusty_slownik(self, tmp_path):
        assert Index.load(tmp_path).entry("nieistniejace") == {}


class TestBlokada:
    def test_drugi_przebieg_jest_odrzucany(self, tmp_path):
        """Blokada jest plikowa, bo `docker compose exec ... run-once` to osobny
        proces niż demon. Dwa przebiegi kasowałyby sobie katalogi robocze."""
        with _exclusive(tmp_path):
            with pytest.raises(AlreadyRunning):
                with _exclusive(tmp_path):
                    pass

    def test_blokada_zwalnia_sie_po_wyjsciu(self, tmp_path):
        with _exclusive(tmp_path):
            pass

        with _exclusive(tmp_path):
            pass  # brak wyjątku = blokada zwolniona

    def test_blokada_zwalnia_sie_takze_po_bledzie(self, tmp_path):
        """Wyjątek w środku przebiegu nie może zostawić zamka na zawsze —
        inaczej kontener przestałby cokolwiek pobierać aż do restartu."""
        with pytest.raises(RuntimeError):
            with _exclusive(tmp_path):
                raise RuntimeError("awaria w trakcie przebiegu")

        with _exclusive(tmp_path):
            pass
