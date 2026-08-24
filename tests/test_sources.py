"""Testy resolvera HTML i walidacji konfiguracji — bez ruszania sieci.

Resolver dostaje podstawioną sesję zwracającą przygotowany HTML, więc każdy
przypadek to kilka linijek znaczników, w których od razu widać, co jest istotne.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from feedsodt.config import AppConfig, ConfigError, SourceConfig, load_sources
from feedsodt.runner import select_sources
from feedsodt.sources import get_resolver


def fake_session(html: str):
    class Session:
        def get(self, url, **kwargs):
            return SimpleNamespace(text=html, raise_for_status=lambda: None)

    return Session()


def resolve(html: str, **options):
    cfg = SourceConfig(
        name="test",
        output="test-gtfs.zip",
        resolver="html",
        select="single",
        options={"url": "https://example.pl/dane", **options},
    )
    return get_resolver("html")(cfg, fake_session(html))


class TestResolverHtml:
    def test_rozpoznaje_po_adresie(self):
        html = """
        <a href="/pliki/regulamin.pdf">Regulamin</a>
        <a href="/pliki/gtfs_01082026.zip">Rozkład</a>
        """
        found = resolve(html, link_pattern=r"gtfs.*\.zip")

        assert [c.url for c in found] == ["https://example.pl/pliki/gtfs_01082026.zip"]

    def test_rozpoznaje_po_podpisie_odnosnika(self):
        """Zielona Góra: obie nazwy plików to hasze, a obok GTFS-a leży 12 MB
        komunikatów głosowych. Jedyne pewne rozróżnienie to podpis."""
        html = """
        <a href="/storage/dla_deweloperow_6bgj1.zip">Komunikaty głosowe</a>
        <a href="/storage/dla_deweloperow_74ib2.zip">Rozkład jazdy w formacie GTFS</a>
        """
        found = resolve(html, link_text="GTFS")

        assert [c.filename for c in found] == ["dla_deweloperow_74ib2.zip"]

    def test_bez_wzorca_bierze_wszystkie_zipy(self):
        html = '<a href="/a.zip">A</a><a href="/b.pdf">B</a>'

        assert len(resolve(html)) == 1

    def test_brak_trafienia_to_czytelny_blad(self):
        """Zmiana układu strony ma się zgłosić wprost, a nie po cichu
        opublikować cokolwiek."""
        from feedsodt.sources import ResolveError

        with pytest.raises(ResolveError, match="zmienił się układ strony"):
            resolve('<a href="/index.html">Start</a>', link_pattern=r"gtfs.*\.zip")

    def test_pomija_powtorzony_adres(self):
        html = '<a href="/a.zip">raz</a><a href="/a.zip">dwa</a>'

        assert len(resolve(html)) == 1


class TestPodpowiedzDaty:
    def test_czyta_date_z_sasiedniej_komorki_wiersza(self):
        """Regresja. W tabeli Wrocławia nazwa pliku stoi w innym <td> niż przycisk.
        find_parent z listą tagów zwracał najbliższy pasujący, czyli <td> z samym
        przyciskiem — i żaden z 69 plików nie dostawał podpowiedzi."""
        html = """
        <table><tr>
          <td>OtwartyWroclaw_rozklad_jazdy_GTFS_22082026</td>
          <td>2026-08-21 11:22:46</td>
          <td>zip</td>
          <td><a href="/hdb/download/127/">Pobierz</a></td>
        </tr></table>
        """
        found = resolve(html, link_pattern=r"/hdb/download/\d+/?$", hint_date=r"(\d{2})(\d{2})(\d{4})")

        assert found[0].hint_start == date(2026, 8, 22)

    def test_kolejnosc_ymd(self):
        """Słupsk nazywa pliki datą publikacji: 2026-07-29.zip."""
        html = '<a href="/files/image/transport/2026-07-29.zip">Pobierz</a>'
        found = resolve(html, hint_date=r"(\d{4})-(\d{2})-(\d{2})", hint_order="ymd")

        assert found[0].hint_start == date(2026, 7, 29)

    def test_niepasujaca_data_nie_wywraca_przebiegu(self):
        """Podpowiedź jest opcjonalna — 23 z 69 plików Wrocławia używa starszej
        konwencji nazw i po prostu jej nie ma."""
        html = '<a href="/pliki/OtwartyWroclaw_GTFS_od 17.01.zip">Pobierz</a>'
        found = resolve(html, hint_date=r"(\d{2})(\d{2})(\d{4})")

        assert found[0].hint_start is None

    def test_bezsensowna_data_nie_wywraca_przebiegu(self):
        html = '<a href="/pliki/gtfs_99999999.zip">Pobierz</a>'
        found = resolve(html, hint_date=r"(\d{2})(\d{2})(\d{4})")

        assert found[0].hint_start is None


class TestKonfiguracja:
    def zapisz(self, tmp_path, tresc: str):
        path = tmp_path / "sources.toml"
        path.write_text(tresc, encoding="utf-8")
        return path

    def test_odrzuca_dwa_zrodla_pod_ta_sama_nazwa_pliku(self, tmp_path):
        """Bez tego dwa miasta nadpisywałyby się nawzajem przy każdym przebiegu."""
        path = self.zapisz(
            tmp_path,
            """
            [[source]]
            name = "a"
            output = "wspolny.zip"
            resolver = "html"
            select = "single"

            [[source]]
            name = "b"
            output = "wspolny.zip"
            resolver = "html"
            select = "single"
            """,
        )

        with pytest.raises(ConfigError, match="tego samego pliku"):
            load_sources(path)

    def test_odrzuca_nieznana_polityke(self, tmp_path):
        path = self.zapisz(
            tmp_path,
            """
            [[source]]
            name = "a"
            output = "a.zip"
            resolver = "html"
            select = "najnowszy"
            """,
        )

        with pytest.raises(ConfigError, match="nieznaną politykę"):
            load_sources(path)

    def test_odrzuca_output_ze_sciezka(self, tmp_path):
        """output trafia prosto do nazwy pliku w katalogu publikacji."""
        path = self.zapisz(
            tmp_path,
            """
            [[source]]
            name = "a"
            output = "../a.zip"
            resolver = "html"
            select = "single"
            """,
        )

        with pytest.raises(ConfigError, match="samą nazwą pliku"):
            load_sources(path)

    def test_wskazuje_brakujace_pole(self, tmp_path):
        path = self.zapisz(
            tmp_path,
            """
            [[source]]
            name = "a"
            output = "a.zip"
            resolver = "html"
            """,
        )

        with pytest.raises(ConfigError, match="select"):
            load_sources(path)

    def test_dodatkowe_klucze_trafiaja_do_opcji_resolvera(self, tmp_path):
        path = self.zapisz(
            tmp_path,
            """
            [[source]]
            name = "a"
            output = "a.zip"
            resolver = "html"
            select = "single"
            link_text = "GTFS"
            """,
        )

        (source,) = load_sources(path)

        assert source.options == {"link_text": "GTFS"}
        assert source.option("link_text") == "GTFS"

    def test_brak_wymaganej_opcji_mowi_ktore_zrodlo(self, tmp_path):
        cfg = SourceConfig(name="rybnik", output="a.zip", resolver="html", select="single")

        with pytest.raises(ConfigError, match="rybnik"):
            cfg.option("url")


class TestHarmonogram:
    """Własna godzina przebiegu dla źródła.

    POLREGIO przechodzi przez przebudowę feedu i routowanie geometrii, więc
    liczy się minutami, a nie sekundami. Wspólny slot z ośmioma miastami
    znaczyłby, że wszystkie czekają na nie bez powodu.
    """

    def zapisz(self, tmp_path, tresc: str):
        path = tmp_path / "sources.toml"
        path.write_text(tresc, encoding="utf-8")
        return path

    def test_slot_zrodla(self, tmp_path):
        path = self.zapisz(
            tmp_path,
            """
            [[source]]
            name = "polregio"
            output = "polregio.zip"
            resolver = "direct"
            select = "single"
            url = "https://example.pl/GTFS.zip"
            run_day = 1
            run_at = "04:00"
            """,
        )

        (source,) = load_sources(path)

        assert source.slot(3, "00:30") == (1, "04:00")

    def test_run_day_zawija_sie_do_dlugosci_cyklu(self, tmp_path):
        """Skrócenie cyklu nie może wysadzić konfiguracji — źródło z dnia 4
        przy cyklu 3-dniowym ma po prostu trafić na dzień 1."""
        cfg = SourceConfig(
            name="a", output="a.zip", resolver="html", select="single", run_day=4, run_at="04:00"
        )

        assert cfg.slot(3, "00:30") == (1, "04:00")

    @pytest.mark.parametrize("wartosc", ["-1", '"wtorek"'])
    def test_zly_run_day_to_czytelny_blad(self, tmp_path, wartosc):
        path = self.zapisz(
            tmp_path,
            f"""
            [[source]]
            name = "a"
            output = "a.zip"
            resolver = "html"
            select = "single"
            run_day = {wartosc}
            """,
        )

        with pytest.raises(ConfigError, match="run_day"):
            load_sources(path)

    def test_wlasna_godzina_i_tryby_ksztaltowania(self, tmp_path):
        path = self.zapisz(
            tmp_path,
            """
            [[source]]
            name = "polregio"
            output = "polregio.zip"
            resolver = "direct"
            select = "single"
            url = "https://example.pl/GTFS.zip"
            transform = "polregio"
            shape = "rail,bus"
            run_at = "02:00"
            """,
        )

        (source,) = load_sources(path)

        assert source.run_at == "02:00"
        assert source.shape == "rail,bus"
        assert source.options == {"url": "https://example.pl/GTFS.zip"}

    def test_bez_run_at_zrodlo_idzie_we_wspolnym_slocie(self, tmp_path):
        path = self.zapisz(
            tmp_path,
            """
            [[source]]
            name = "rybnik"
            output = "rybnik.zip"
            resolver = "html"
            select = "single"
            url = "https://example.pl"
            """,
        )

        (source,) = load_sources(path)

        assert source.run_at == ""
        assert source.schedule("00:30") == "00:30"

    @pytest.mark.parametrize("wartosc", ["2:00", "24:00", "02:60", "rano", "0200"])
    def test_zla_godzina_to_czytelny_blad(self, tmp_path, wartosc):
        path = self.zapisz(
            tmp_path,
            f"""
            [[source]]
            name = "a"
            output = "a.zip"
            resolver = "html"
            select = "single"
            run_at = "{wartosc}"
            """,
        )

        with pytest.raises(ConfigError, match="HH:MM"):
            load_sources(path)

    @staticmethod
    def aplikacja(tmp_path, sources, interval_days=3):
        return AppConfig(
            sources=sources,
            output_dir=tmp_path,
            work_dir=tmp_path,
            cache_dir=tmp_path,
            config_dir=tmp_path,
            run_at="00:30",
            interval_days=interval_days,
            run_on_start=False,
            timezone="Europe/Warsaw",
        )

    def test_zrodla_grupuja_sie_po_slocie(self, tmp_path):
        sources = (
            SourceConfig(name="a", output="a.zip", resolver="html", select="single"),
            SourceConfig(name="b", output="b.zip", resolver="html", select="single", run_at="01:00"),
            SourceConfig(
                name="polregio", output="p.zip", resolver="direct", select="single",
                run_day=1, run_at="04:00",
            ),
            SourceConfig(
                name="wylaczone", output="w.zip", resolver="html", select="single", enabled=False
            ),
        )
        grupy = {
            slot: [cfg.name for cfg in group]
            for slot, group in self.aplikacja(tmp_path, sources).schedules().items()
        }

        assert grupy == {(0, "00:30"): ["a"], (0, "01:00"): ["b"], (1, "04:00"): ["polregio"]}

    def test_kazda_godzina_to_jedno_zadanie_cron(self, tmp_path):
        """Dzień cyklu rozstrzyga się przy odpaleniu, więc źródła z różnych dni
        o tej samej godzinie dzielą jedno zadanie."""
        sources = (
            SourceConfig(name="a", output="a.zip", resolver="html", select="single", run_at="04:00"),
            SourceConfig(
                name="b", output="b.zip", resolver="html", select="single", run_day=1, run_at="04:00"
            ),
        )

        assert self.aplikacja(tmp_path, sources).hours() == ["04:00"]

    def test_przebieg_bierze_tylko_swoj_slot(self, tmp_path):
        sources = (
            SourceConfig(name="a", output="a.zip", resolver="html", select="single", run_at="04:00"),
            SourceConfig(
                name="b", output="b.zip", resolver="direct", select="single", run_day=1, run_at="04:00"
            ),
        )
        app = self.aplikacja(tmp_path, sources)
        dzien0 = date(2026, 1, 1)  # CYCLE_ANCHOR

        assert [c.name for c in select_sources(app, at="04:00", today=dzien0)] == ["a"]
        assert [c.name for c in select_sources(app, at="04:00", today=dzien0 + timedelta(days=1))] == ["b"]
        assert [c.name for c in select_sources(app, at="04:00", today=dzien0 + timedelta(days=2))] == []
        assert [c.name for c in select_sources(app, at="04:00", today=dzien0 + timedelta(days=3))] == ["a"]
        # Ponowienie i ręczne run-once idą po nazwach, niezależnie od slotu.
        assert [c.name for c in select_sources(app, only={"b"})] == ["b"]
        assert len(select_sources(app)) == 2

    def test_cykl_jest_liczony_od_stalej_daty(self, tmp_path):
        """Nie od startu kontenera: restart w środku cyklu nie może przesunąć
        kolejki, a maszyna zapasowa ma liczyć tak samo."""
        app = self.aplikacja(tmp_path, ())

        assert app.cycle_day(date(2026, 1, 1)) == 0
        assert app.cycle_day(date(2026, 1, 4)) == 0
        assert app.cycle_day(date(2026, 1, 5)) == 1
        # Przełom miesiąca nie robi krótszego odstępu — to pułapka `day=*/3`.
        assert app.cycle_day(date(2026, 1, 31)) + 1 == app.cycle_day(date(2026, 2, 1))

    def test_cykl_jednodniowy_to_codziennie(self, tmp_path):
        sources = (
            SourceConfig(name="a", output="a.zip", resolver="html", select="single", run_at="04:00"),
        )
        app = self.aplikacja(tmp_path, sources, interval_days=1)

        for offset in range(3):
            wybrane = select_sources(app, at="04:00", today=date(2026, 1, 1) + timedelta(days=offset))
            assert [c.name for c in wybrane] == ["a"]
