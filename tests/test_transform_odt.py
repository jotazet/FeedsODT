"""Testy lekkiej transformacji `odt`.

Ta transformacja obsługuje feedy, które są poprawne — brakuje im tylko barw
linii i właściwego wystawcy. Sprawdzamy więc dwie rzeczy: że barwy trafiają
tam, gdzie mają, i że nic poza `routes.txt` i `feed_info.txt` nie zostaje
ruszone.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from conftest import read_gtfs, write_gtfs
from feedsodt import feedinfo
from feedsodt.colors import ColorError, normalize, text_color_for
from feedsodt.transform import Context, TransformError, get_transform
from feedsodt.transform.odt import load_colors, rebuild

ROUTES = [
    {
        "route_id": "1359_A1",
        "agency_id": "KML",
        "route_short_name": "A1",
        "route_long_name": "Kraków — Myślenice",
        "route_type": "3",
        "route_color": "",
        "route_text_color": "",
        "route_sort_order": "1",
    },
    {
        "route_id": "1359_A2",
        "agency_id": "KML",
        "route_short_name": "A2",
        "route_long_name": "Myślenice — Wieliczka",
        "route_type": "3",
        "route_color": "",
        "route_text_color": "",
        "route_sort_order": "2",
    },
]


def feed(tmp_path: Path, routes=None, extra=None, with_feed_info=True) -> Path:
    tables = {
        "agency.txt": [
            {
                "agency_id": "KML",
                "agency_name": "Koleje Małopolskie sp. z o.o.",
                "agency_url": "https://kolejemalopolskie.com.pl",
                "agency_timezone": "Europe/Warsaw",
            }
        ],
        "stops.txt": [
            {"stop_id": "S1", "stop_name": "A", "stop_lat": "50.06", "stop_lon": "19.94"},
            {"stop_id": "S2", "stop_name": "B", "stop_lat": "50.07", "stop_lon": "19.95"},
        ],
        "routes.txt": routes if routes is not None else ROUTES,
        "trips.txt": [
            {"route_id": "1359_A1", "service_id": "S1", "trip_id": "T1", "shape_id": ""}
        ],
        "stop_times.txt": [
            {
                "trip_id": "T1",
                "arrival_time": "08:00:00",
                "departure_time": "08:00:00",
                "stop_id": "S1",
                "stop_sequence": "1",
            }
        ],
        "calendar.txt": [
            {"service_id": "S1", "start_date": "20260821", "end_date": "20260930"}
        ],
    }
    if with_feed_info:
        tables["feed_info.txt"] = [
            {
                "feed_publisher_name": "R&G PLUS Sp. z o.o.",
                "feed_publisher_url": "http://www.rg.com.pl",
                "feed_lang": "pl",
                "feed_start_date": "20260821",
                "feed_end_date": "20260930",
            }
        ]
    path = write_gtfs(tmp_path / "zrodlo.zip", tables)
    if extra:
        with zipfile.ZipFile(path, "a") as archive:
            for name, payload in extra.items():
                archive.writestr(name, payload)
    return path


def ctx(tmp_path: Path, **options) -> Context:
    return Context(config_dir=tmp_path, options=options)


def colors_file(tmp_path: Path, body: str, name: str = "barwy.txt") -> str:
    directory = tmp_path / "colors"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")
    return name


class TestKolory:
    def test_jedna_barwa_dla_wszystkich_linii(self, tmp_path):
        dest = tmp_path / "out.zip"
        rebuild(feed(tmp_path), dest, ctx(tmp_path, route_color="0462A2"))

        routes = read_gtfs(dest, "routes.txt")

        assert {r["route_color"] for r in routes} == {"0462A2"}
        assert {r["route_text_color"] for r in routes} == {"FFFFFF"}

    def test_barwa_per_linia_z_tabeli(self, tmp_path):
        name = colors_file(
            tmp_path,
            "route_id,route_short_name,route_color\nMLDa1,A1,b22159\nMLDa2,A2,c0000f\n",
        )
        dest = tmp_path / "out.zip"
        rebuild(feed(tmp_path), dest, ctx(tmp_path, colors_file=name))

        barwy = {r["route_short_name"]: r["route_color"] for r in read_gtfs(dest, "routes.txt")}

        assert barwy == {"A1": "B22159", "A2": "C0000F"}

    def test_dopasowanie_po_short_name_a_nie_po_id(self, tmp_path):
        """route_id niesie u MLD numer publikacji (`1359_A1`) i zmienia się co
        wydanie, więc tabela barw musi trzymać się oznaczenia linii."""
        name = colors_file(tmp_path, "route_short_name,route_color\nA1,b22159\n")
        dest = tmp_path / "out.zip"
        rebuild(feed(tmp_path), dest, ctx(tmp_path, colors_file=name))

        barwy = {r["route_id"]: r["route_color"] for r in read_gtfs(dest, "routes.txt")}

        assert barwy["1359_A1"] == "B22159"

    def test_tabela_wygrywa_z_barwa_domyslna(self, tmp_path):
        name = colors_file(tmp_path, "route_short_name,route_color\nA1,29ace0\n")
        dest = tmp_path / "out.zip"
        stats = rebuild(feed(tmp_path), dest, ctx(tmp_path, colors_file=name, route_color="164194"))

        barwy = {r["route_short_name"]: r["route_color"] for r in read_gtfs(dest, "routes.txt")}

        assert barwy == {"A1": "29ACE0", "A2": "164194"}
        assert stats.colored_from_file == 1
        assert stats.colored_default == 1
        assert stats.unmatched == ["A2"]

    def test_kratka_i_wielkosc_liter_nie_maja_znaczenia(self, tmp_path):
        dest = tmp_path / "out.zip"
        rebuild(feed(tmp_path), dest, ctx(tmp_path, route_color="#0462a2"))

        assert read_gtfs(dest, "routes.txt")[0]["route_color"] == "0462A2"

    def test_kolor_napisu_dobiera_sie_do_tla(self, tmp_path):
        dest = tmp_path / "out.zip"
        rebuild(feed(tmp_path), dest, ctx(tmp_path, route_color="E6B800"))

        assert read_gtfs(dest, "routes.txt")[0]["route_text_color"] == "000000"

    def test_bez_konfiguracji_barw_to_czytelny_blad(self, tmp_path):
        with pytest.raises(TransformError, match="route_color albo colors_file"):
            rebuild(feed(tmp_path), tmp_path / "out.zip", ctx(tmp_path))

    def test_zla_barwa_w_tabeli_wskazuje_linie(self, tmp_path):
        name = colors_file(tmp_path, "route_short_name,route_color\nA1,niebieski\n")

        with pytest.raises(TransformError, match="A1"):
            rebuild(feed(tmp_path), tmp_path / "out.zip", ctx(tmp_path, colors_file=name))

    def test_brak_tabeli_mowi_gdzie_jej_szukano(self, tmp_path):
        with pytest.raises(TransformError, match="tabeli kolorów"):
            rebuild(feed(tmp_path), tmp_path / "out.zip", ctx(tmp_path, colors_file="nie-ma.txt"))


class TestTabelaBarw:
    def test_komentarze_i_puste_wiersze_sa_pomijane(self, tmp_path):
        path = tmp_path / "barwy.txt"
        path.write_text(
            "# barwy SKA, te same co w POLREGIO\n\nroute_short_name,route_color\nSKA1,29ace0\n",
            encoding="utf-8",
        )

        assert load_colors(path) == {"SKA1": "29ACE0"}

    def test_wiersze_bez_barwy_sa_pomijane(self, tmp_path):
        """Plik od organizatora bywa niekompletny — ma wejść bez czyszczenia."""
        path = tmp_path / "barwy.txt"
        path.write_text("route_short_name,route_color\nA1,b22159\nA2,\n", encoding="utf-8")

        assert load_colors(path) == {"A1": "B22159"}

    def test_tabela_bez_ani_jednej_barwy_to_blad(self, tmp_path):
        path = tmp_path / "barwy.txt"
        path.write_text("route_short_name,route_color\nA1,\n", encoding="utf-8")

        with pytest.raises(TransformError, match="ani jednej"):
            load_colors(path)

    def test_wersjonowane_tabele_projektu_sa_poprawne(self):
        """config/colors/ jest konfiguracją, więc literówka w barwie ma się
        wywalić w testach, a nie o 02:00 w nocy."""
        for path in sorted(Path("config/colors").glob("*.txt")):
            assert load_colors(path), path


class TestWystawca:
    def test_feed_info_wskazuje_odt(self, tmp_path):
        dest = tmp_path / "out.zip"
        rebuild(feed(tmp_path), dest, ctx(tmp_path, route_color="0462A2"))

        info = read_gtfs(dest, "feed_info.txt")[0]

        assert info["feed_publisher_name"] == feedinfo.PUBLISHER_NAME
        assert info["feed_publisher_url"] == feedinfo.PUBLISHER_URL
        assert info["feed_contact_email"] == feedinfo.CONTACT_EMAIL

    def test_przewoznik_zostaje_w_agency(self, tmp_path):
        """Wystawcą paczki jesteśmy my, ale przewoźnikiem nadal on."""
        dest = tmp_path / "out.zip"
        rebuild(feed(tmp_path), dest, ctx(tmp_path, route_color="0462A2"))

        assert read_gtfs(dest, "agency.txt")[0]["agency_name"] == "Koleje Małopolskie sp. z o.o."

    def test_okno_przepisane_z_oryginalu(self, tmp_path):
        dest = tmp_path / "out.zip"
        rebuild(feed(tmp_path), dest, ctx(tmp_path, route_color="0462A2"))

        info = read_gtfs(dest, "feed_info.txt")[0]

        assert (info["feed_start_date"], info["feed_end_date"]) == ("20260821", "20260930")

    def test_bez_feed_info_okno_wychodzi_z_kalendarza(self, tmp_path):
        dest = tmp_path / "out.zip"
        rebuild(feed(tmp_path, with_feed_info=False), dest, ctx(tmp_path, route_color="0462A2"))

        info = read_gtfs(dest, "feed_info.txt")[0]

        assert (info["feed_start_date"], info["feed_end_date"]) == ("20260821", "20260930")


class TestReszta:
    def test_pozostale_tabele_ida_bajt_w_bajt(self, tmp_path):
        source = feed(tmp_path)
        dest = tmp_path / "out.zip"
        rebuild(source, dest, ctx(tmp_path, route_color="0462A2"))

        with zipfile.ZipFile(source) as a, zipfile.ZipFile(dest) as b:
            for name in ("agency.txt", "stops.txt", "trips.txt", "stop_times.txt", "calendar.txt"):
                assert a.read(name) == b.read(name), name

    def test_kolumny_routes_sa_zachowane(self, tmp_path):
        """Feedy niosą tu pola, których nie rozumiemy (route_sort_order,
        continuous_pickup) i nie ma powodu ich gubić."""
        dest = tmp_path / "out.zip"
        rebuild(feed(tmp_path), dest, ctx(tmp_path, route_color="0462A2"))

        assert "route_sort_order" in read_gtfs(dest, "routes.txt")[0]

    def test_pliki_spoza_gtfs_wypadaja(self, tmp_path):
        """Feed SKA jest pakowany narzędziem Microsoftu i niesie
        [Content_Types].xml, którego w GTFS-ie nie ma."""
        source = feed(tmp_path, extra={"[Content_Types].xml": "<Types/>"})
        dest = tmp_path / "out.zip"
        stats = rebuild(source, dest, ctx(tmp_path, route_color="0462A2"))

        with zipfile.ZipFile(dest) as archive:
            assert "[Content_Types].xml" not in archive.namelist()
        assert stats.dropped_files == ["[Content_Types].xml"]

    def test_determinizm(self, tmp_path):
        source = feed(tmp_path)
        first, second = tmp_path / "a.zip", tmp_path / "b.zip"
        rebuild(source, first, ctx(tmp_path, route_color="0462A2"))
        rebuild(source, second, ctx(tmp_path, route_color="0462A2"))

        assert first.read_bytes() == second.read_bytes()

    def test_brak_wymaganej_tabeli_to_czytelny_blad(self, tmp_path):
        niepelny = write_gtfs(tmp_path / "zle.zip", {"agency.txt": [{"agency_id": "X"}]})

        with pytest.raises(TransformError, match="brakuje"):
            rebuild(niepelny, tmp_path / "out.zip", ctx(tmp_path, route_color="0462A2"))


class TestRejestr:
    def test_transformacja_jest_dostepna_po_nazwie(self):
        assert get_transform("odt") is rebuild


class TestNormalizacjaBarw:
    @pytest.mark.parametrize("value", ["#0462A2", "0462a2", " 0462A2 "])
    def test_rownowazne_zapisy(self, value):
        assert normalize(value) == "0462A2"

    @pytest.mark.parametrize("value", ["", "12345", "zielony", "#GGGGGG"])
    def test_co_nie_jest_barwa(self, value):
        with pytest.raises(ColorError):
            normalize(value)

    def test_kontrast_napisu(self):
        assert text_color_for("FAF40E") == "000000"  # S34, jaskrawy żółty
        assert text_color_for("164194") == "FFFFFF"  # domyślny granat SKA
