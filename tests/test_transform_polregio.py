"""Testy przebudowy feedu POLREGIO.

Wejście jest budowane ręcznie tak, żeby odtworzyć wady oryginału: puste nazwy
tras, brak kierunkowskazów, osobna trasa na każdy numer pociągu, autobus
zastępczy oznaczony jako kolej i perony bez własnych współrzędnych.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from conftest import read_gtfs, write_gtfs
from feedsodt.transform import TransformError, get_transform
from feedsodt.transform.polregio import (
    CATEGORY_COLORS,
    POLREGIO_RED,
    normalize_category,
    rebuild,
    text_color_for,
)

#: Prawdziwe nazwy stacji, bo współrzędne peronów są wyszukiwane po nazwie
#: w wersjonowanej liście PLK (feedsodt/data/platforms-list.json).
STOPS = [
    {
        "stop_name": "Gdynia Główna",
        "stop_id": "G1",
        "stop_lat": "54.5200",
        "stop_lon": "18.5300",
        "platform_code": "I/1",
    },
    {
        "stop_name": "Gdynia Główna",
        "stop_id": "G2",
        "stop_lat": "54.5200",
        "stop_lon": "18.5300",
        "platform_code": "II/2",
    },
    {
        "stop_name": "Reda",
        "stop_id": "R1",
        "stop_lat": "54.6000",
        "stop_lon": "18.3500",
        "platform_code": "",
    },
    {
        "stop_name": "Hel",
        "stop_id": "H1",
        "stop_lat": "54.6000",
        "stop_lon": "18.8000",
        "platform_code": "I/1",
    },
]


def feed(tmp_path: Path, trasy: list[tuple[str, str, list[str]]]) -> Path:
    """Buduje feed w formacie Polregio.

    `trasy` to lista (route_id, kategoria, sekwencja stop_id) — po jednym kursie
    na trasę, tak jak w oryginale.
    """
    routes, trips, stop_times = [], [], []
    for route_id, kategoria, sekwencja in trasy:
        routes.append(
            {
                "route_id": route_id,
                "route_short_name": kategoria,
                "route_long_name": "",
                "route_desc": "",
                "route_type": "2",
                "route_color": "",
                "route_text_color": "",
                "agency_id": "PR",
            }
        )
        trip_id = f"T-{route_id}"
        trips.append(
            {
                "route_id": route_id,
                "service_id": "S1",
                "trip_id": trip_id,
                "trip_headsign": "",
                "block_id": "",
                "trip_short_name": f"{route_id} ",  # ogon spacji jak w oryginale
                "wheelchair_accessible": "0",
                "bikes_allowed": "1",
            }
        )
        for index, stop in enumerate(sekwencja, start=1):
            stop_times.append(
                {
                    "trip_id": trip_id,
                    "arrival_time": f"{7 + index:02d}:00:00",
                    "departure_time": f"{7 + index:02d}:00:00",
                    "stop_id": stop,
                    "stop_sequence": str(index),
                }
            )

    return write_gtfs(
        tmp_path / "polregio.zip",
        {
            "agency.txt": [
                {
                    "agency_id": "PR",
                    "agency_name": "POLREGIO",
                    "agency_url": "https://www.polregio.pl/",
                    "agency_timezone": "Europe/Warsaw",
                }
            ],
            "stops.txt": STOPS,
            "routes.txt": routes,
            "trips.txt": trips,
            "stop_times.txt": stop_times,
            "calendar.txt": [
                {
                    "service_id": "S1",
                    "start_date": "20260302",
                    "end_date": "20261212",
                    "monday": "1",
                    "tuesday": "1",
                    "wednesday": "1",
                    "thursday": "1",
                    "friday": "1",
                    "saturday": "1",
                    "sunday": "1",
                }
            ],
        },
    )


@pytest.fixture
def przebudowany(tmp_path):
    """Cztery pociągi tam i z powrotem na tej samej relacji — w oryginale
    to cztery osobne trasy."""
    source = feed(
        tmp_path,
        [
            ("R-1-01-51-R", "R", ["G1", "R1", "H1"]),
            ("R-2-01-51-R", "R", ["G1", "R1", "H1"]),
            ("R-3-01-51-R", "R", ["H1", "R1", "G2"]),
            ("R-4-01-51-R", "R", ["H1", "R1", "G2"]),
        ],
    )
    dest = tmp_path / "fixed.zip"
    rebuild(source, dest)
    return dest


class TestKonsolidacja:
    def test_trasa_to_kategoria(self, przebudowany):
        """Sedno naprawy: 4 trasy na numer pociągu -> 1 kategoria."""
        routes = read_gtfs(przebudowany, "routes.txt")

        assert len(routes) == 1
        assert routes[0]["route_id"] == "PR-R"
        assert routes[0]["route_short_name"] == "R"
        assert routes[0]["route_long_name"] == "REGIO"

    def test_rozne_relacje_tej_samej_kategorii_to_jedna_trasa(self, tmp_path):
        """Gdynia — Hel i Gdynia — Reda jadą jako REGIO, więc trafiają do trasy
        `R`. Relację niesie kierunkowskaz kursu, nie osobny wiersz w routes.txt."""
        source = feed(
            tmp_path,
            [
                ("R-1-01-51-R", "R", ["G1", "H1"]),
                ("R-2-01-51-R", "R", ["G1", "R1"]),
            ],
        )
        dest = tmp_path / "fixed.zip"
        rebuild(source, dest)

        routes = read_gtfs(dest, "routes.txt")
        headsigny = {t["trip_short_name"]: t["trip_headsign"] for t in read_gtfs(dest, "trips.txt")}

        assert len(routes) == 1
        assert headsigny == {"R-1-01-51-R": "Hel", "R-2-01-51-R": "Reda"}

    def test_rozne_kategorie_nie_sa_laczone(self, tmp_path):
        """Ta sama relacja obsługiwana przez REGIO i przez autobus zastępczy
        to dwie różne trasy — inaczej pasażer nie odróżni pociągu od autobusu."""
        source = feed(
            tmp_path,
            [
                ("R-1-01-51-R", "R", ["G1", "H1"]),
                ("R-2-01-51-BUS", "BUS", ["G1", "H1"]),
            ],
        )
        dest = tmp_path / "fixed.zip"
        rebuild(source, dest)

        routes = read_gtfs(dest, "routes.txt")

        assert len(routes) == 2
        assert {r["route_short_name"] for r in routes} == {"R", "BUS"}

    def test_kierunek_trafia_do_direction_id(self, przebudowany):
        """direction_id liczy się nadal z relacji, choć relacji nie ma już
        w routes.txt — bez tego oba kierunki byłyby nierozróżnialne."""
        trips = read_gtfs(przebudowany, "trips.txt")
        kierunki = {t["trip_id"]: t["direction_id"] for t in trips}

        assert kierunki["T-R-1-01-51-R"] == "0"  # Gdynia -> Hel
        assert kierunki["T-R-3-01-51-R"] == "1"  # Hel -> Gdynia
        assert sorted(set(kierunki.values())) == ["0", "1"]


class TestWariantPrzyspieszony:
    """Sufiks „P" znaczy „przyspieszony" — to wariant tej samej linii, nie
    osobna linia. Przewoźnik wystawia go jako oddzielną kategorię, więc ten sam
    pociąg jedzie najpierw jako K33P, potem jako K33: planer pokazuje dwie linie
    w dwóch kolorach tam, gdzie pasażer widzi jedną."""

    @pytest.mark.parametrize(
        "kategoria, oczekiwana",
        [("K33P", "K33"), ("RP", "R"), ("K7P", "K7"), ("S31P", "S31"), ("M7P", "M7")],
    )
    def test_sufiks_p_znika(self, kategoria, oczekiwana):
        assert normalize_category(kategoria) == oczekiwana

    @pytest.mark.parametrize(
        "kategoria", ["K33", "R", "BUS", "IR", "Os", "SKA3", "sR", "K7 BUS", "K22"]
    )
    def test_reszta_zostaje_nietknieta(self, kategoria):
        """Wzorzec jest wąski celowo — nie wszystko, co kończy się na P, jest
        wariantem przyspieszonym."""
        assert normalize_category(kategoria) == kategoria

    def test_wariant_przyspieszony_ląduje_w_tej_samej_trasie(self, tmp_path):
        source = feed(
            tmp_path,
            [
                ("R-1-01-51-K33", "K33", ["G1", "H1"]),
                ("R-2-01-51-K33P", "K33P", ["G1", "R1"]),
            ],
        )
        dest = tmp_path / "fixed.zip"
        rebuild(source, dest)

        routes = read_gtfs(dest, "routes.txt")

        assert len(routes) == 1
        assert routes[0]["route_short_name"] == "K33"

    def test_wariant_przyspieszony_dostaje_barwę_linii_podstawowej(self, tmp_path):
        """To był widoczny objaw: ten sam pociąg w dwóch kolorach."""
        source = feed(tmp_path, [("R-1-01-51-K33P", "K33P", ["G1", "H1"])])
        dest = tmp_path / "fixed.zip"
        rebuild(source, dest)

        assert read_gtfs(dest, "routes.txt")[0]["route_color"] == CATEGORY_COLORS["K33"]


class TestUzupelnienia:
    def test_kierunkowskaz_to_stacja_docelowa(self, przebudowany):
        """W oryginale trip_headsign jest pusty we wszystkich 39766 kursach."""
        trips = read_gtfs(przebudowany, "trips.txt")
        headsigny = {t["trip_id"]: t["trip_headsign"] for t in trips}

        assert headsigny["T-R-1-01-51-R"] == "Hel"
        assert headsigny["T-R-3-01-51-R"] == "Gdynia Główna"
        assert all(t["trip_headsign"] for t in trips)

    def test_obcina_ogon_spacji_z_numeru_pociagu(self, przebudowany):
        trips = read_gtfs(przebudowany, "trips.txt")

        assert all(t["trip_short_name"] == t["trip_short_name"].strip() for t in trips)
        assert trips[0]["trip_short_name"]

    def test_dokłada_feed_info(self, przebudowany):
        """Oryginał go nie ma, więc okno obowiązywania trzeba było zgadywać."""
        info = read_gtfs(przebudowany, "feed_info.txt")[0]

        assert info["feed_start_date"] == "20260302"
        assert info["feed_end_date"] == "20261212"

    def test_wystawca_to_odt_a_nie_przewoznik(self, przebudowany):
        """Publikujemy tę paczkę pod własnym, stałym adresem, więc zgłoszenia
        o błędach mają trafiać do nas. Przewoźnik zostaje w agency.txt."""
        info = read_gtfs(przebudowany, "feed_info.txt")[0]
        agency = read_gtfs(przebudowany, "agency.txt")[0]

        assert info["feed_publisher_name"] == "Otwarte Dane Transportowe"
        assert info["feed_publisher_url"] == "https://odt.org.pl/"
        assert info["feed_contact_email"] == "kontakt@odt.org.pl"
        assert agency["agency_name"] == "POLREGIO"

    def test_nazwa_tylko_dla_znanych_kategorii(self, tmp_path):
        """Kody linii typu K33 świadomie nie dostają zmyślonego rozwinięcia."""
        source = feed(
            tmp_path,
            [("R-1-01-51-R", "R", ["G1", "H1"]), ("R-2-01-51-K33", "K33", ["G1", "R1"])],
        )
        dest = tmp_path / "fixed.zip"
        rebuild(source, dest)

        nazwy = {r["route_short_name"]: r["route_long_name"] for r in read_gtfs(dest, "routes.txt")}

        assert nazwy["R"] == "REGIO"
        assert nazwy["K33"] == ""


class TestPerony:
    def test_perony_dostaja_wlasne_wspolrzedne(self, przebudowany):
        """W oryginale peron I/1 i II/2 mają współrzędne budynku dworca."""
        stops = {s["stop_id"]: s for s in read_gtfs(przebudowany, "stops.txt")}

        pierwszy = (stops["G1"]["stop_lat"], stops["G1"]["stop_lon"])
        drugi = (stops["G2"]["stop_lat"], stops["G2"]["stop_lon"])

        assert pierwszy != drugi
        assert pierwszy != ("54.5200", "18.5300")
        # Peron nie może uciec od stacji dalej niż o kilkaset metrów.
        assert abs(float(stops["G1"]["stop_lat"]) - 54.52) < 0.01

    def test_przystanek_bez_oznaczenia_peronu_zostaje_przy_stacji(self, przebudowany):
        """Reda ma puste platform_code — nie ma czego szukać na liście PLK."""
        stops = {s["stop_id"]: s for s in read_gtfs(przebudowany, "stops.txt")}

        assert (stops["R1"]["stop_lat"], stops["R1"]["stop_lon"]) == ("54.6000", "18.3500")

    def test_perony_wisza_pod_stacja(self, przebudowany):
        stops = read_gtfs(przebudowany, "stops.txt")
        stacje = {s["stop_id"]: s for s in stops if s["location_type"] == "1"}
        perony = [s for s in stops if s["stop_name"] == "Gdynia Główna" and s["location_type"] == "0"]

        assert len(stacje) == 1  # tylko Gdynia ma dwa przystanki do zgrupowania
        assert {s["parent_station"] for s in perony} == set(stacje)
        assert all(s["platform_code"] for s in perony)

    def test_stacja_o_jednym_przystanku_nie_dostaje_wezla_nadrzednego(self, przebudowany):
        """Sztuczna stacja nad pojedynczym przystankiem to czysty balast."""
        stops = {s["stop_id"]: s for s in read_gtfs(przebudowany, "stops.txt")}

        assert stops["H1"]["parent_station"] == ""
        assert stops["H1"]["location_type"] == "0"

    def test_nieuzywane_przystanki_wypadaja(self, tmp_path):
        """W oryginale 1451 z 5696 przystanków to pinezki, przy których nic
        się nie zatrzymuje."""
        source = feed(tmp_path, [("R-1-01-51-R", "R", ["G1", "H1"])])
        dest = tmp_path / "fixed.zip"
        rebuild(source, dest)

        zostaly = {s["stop_id"] for s in read_gtfs(dest, "stops.txt")}

        assert zostaly == {"G1", "H1"}

    def test_stop_times_wskazuje_wylacznie_na_przystanki(self, przebudowany):
        """Kurs nie może zatrzymywać się na węźle location_type=1."""
        stops = {s["stop_id"]: s for s in read_gtfs(przebudowany, "stops.txt")}

        for row in read_gtfs(przebudowany, "stop_times.txt"):
            assert stops[row["stop_id"]]["location_type"] == "0"


class TestTypyIKolory:
    def test_autobus_zastepczy_dostaje_typ_autobusowy(self, tmp_path):
        """Regresja na realny błąd: w oryginale kategorie `K7 BUS` i `K22 BUS`
        miały route_type=2 (kolej), choć zwykłe `BUS` miało poprawnie 3."""
        source = feed(
            tmp_path,
            [
                ("R-1-01-51-R", "R", ["G1", "H1"]),
                ("R-2-01-51-BUS", "BUS", ["G1", "R1"]),
                ("R-3-01-51-K7BUS", "K7 BUS", ["R1", "H1"]),
            ],
        )
        dest = tmp_path / "fixed.zip"
        rebuild(source, dest)

        typy = {r["route_short_name"]: r["route_type"] for r in read_gtfs(dest, "routes.txt")}

        assert typy == {"R": "2", "BUS": "3", "K7 BUS": "3"}

    def test_kolor_firmowy_gdy_kategoria_nie_ma_wlasnego(self, przebudowany):
        routes = read_gtfs(przebudowany, "routes.txt")

        assert all(r["route_color"] == POLREGIO_RED for r in routes)
        assert all(r["route_text_color"] == "FFFFFF" for r in routes)

    def test_kategoria_z_wlasnym_kolorem_go_dostaje(self, tmp_path):
        """Kody organizatorów wojewódzkich mają barwy, których pasażer szuka
        na mapie — K7 jest zielone, K22 zielonożółte, nie firmowo czerwone."""
        source = feed(
            tmp_path,
            [
                ("R-1-01-51-K7", "K7", ["G1", "H1"]),
                ("R-2-01-51-K22", "K22", ["G1", "R1"]),
                ("R-3-01-51-R", "R", ["R1", "H1"]),
            ],
        )
        dest = tmp_path / "fixed.zip"
        rebuild(source, dest)

        kolory = {r["route_short_name"]: r["route_color"] for r in read_gtfs(dest, "routes.txt")}

        assert kolory["K7"] == CATEGORY_COLORS["K7"]
        assert kolory["K22"] == CATEGORY_COLORS["K22"]
        assert kolory["R"] == POLREGIO_RED

    def test_kazdy_kolor_z_tabeli_jest_poprawnym_szesnastkowym(self):
        for kategoria, kolor in CATEGORY_COLORS.items():
            assert len(kolor) == 6, kategoria
            assert kolor == kolor.upper(), kategoria
            int(kolor, 16)  # rzuci ValueError, gdy to nie jest kolor

    def test_kolor_tekstu_dobiera_sie_do_jasnosci_tla(self):
        """Dzięki temu podmiana koloru kategorii nie wymaga pamiętania
        o drugim polu."""
        assert text_color_for(POLREGIO_RED) == "FFFFFF"
        assert text_color_for("000000") == "FFFFFF"
        assert text_color_for("FFFFFF") == "000000"
        assert text_color_for("F38C33") == "000000"


class TestIntegralnosc:
    def test_brak_sierot_po_przemapowaniu(self, przebudowany):
        routes = {r["route_id"] for r in read_gtfs(przebudowany, "routes.txt")}
        trips = read_gtfs(przebudowany, "trips.txt")
        trip_ids = {t["trip_id"] for t in trips}
        stop_times = read_gtfs(przebudowany, "stop_times.txt")
        stops = {s["stop_id"] for s in read_gtfs(przebudowany, "stops.txt")}

        assert {t["route_id"] for t in trips} <= routes
        assert {s["trip_id"] for s in stop_times} <= trip_ids
        assert {s["stop_id"] for s in stop_times} <= stops

    def test_stacja_nadrzedna_nie_koliduje_z_istniejacym_id(self, tmp_path):
        """Identyfikator stacji jest generowany, więc musi ustąpić, gdy taki
        stop_id już w feedzie jest."""
        stops = STOPS + [
            {
                "stop_name": "Kolizja",
                "stop_id": "ST-GDYNIA_GLOWNA",
                "stop_lat": "54.0",
                "stop_lon": "18.0",
                "platform_code": "",
            }
        ]
        source = feed(tmp_path, [("R-1-01-51-R", "R", ["G1", "G2", "ST-GDYNIA_GLOWNA"])])
        with zipfile.ZipFile(source, "a") as archive:
            pass
        source = write_gtfs(
            tmp_path / "kolizja.zip",
            {
                **{
                    name: read_gtfs(source, name)
                    for name in ("agency.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt")
                },
                "stops.txt": stops,
            },
        )
        dest = tmp_path / "fixed.zip"
        rebuild(source, dest)

        ids = [s["stop_id"] for s in read_gtfs(dest, "stops.txt")]

        assert len(ids) == len(set(ids))
        assert "ST-GDYNIA_GLOWNA" in ids
        assert "ST-GDYNIA_GLOWNA-2" in ids

    def test_tabele_nietykane_sa_kopiowane_bez_zmian(self, tmp_path, przebudowany):
        """stop_times, calendar i agency mają przejść bajt w bajt. stops.txt jest
        przebudowywany, więc go tu nie ma."""
        source = tmp_path / "polregio.zip"
        with zipfile.ZipFile(source) as a, zipfile.ZipFile(przebudowany) as b:
            for name in ("stop_times.txt", "calendar.txt", "agency.txt"):
                assert a.read(name) == b.read(name), name

    def test_determinizm(self, tmp_path):
        """Publikacja opiera się na sha256, więc dwie przebudowy tego samego
        wejścia muszą dać identyczny plik."""
        source = feed(tmp_path, [("R-1-01-51-R", "R", ["G1", "H1"])])
        pierwszy, drugi = tmp_path / "a.zip", tmp_path / "b.zip"
        rebuild(source, pierwszy)
        rebuild(source, drugi)

        assert pierwszy.read_bytes() == drugi.read_bytes()

    def test_brak_wymaganej_tabeli_to_czytelny_blad(self, tmp_path):
        niepelny = write_gtfs(tmp_path / "zle.zip", {"agency.txt": [{"agency_id": "PR"}]})

        with pytest.raises(TransformError, match="brakuje"):
            rebuild(niepelny, tmp_path / "fixed.zip")


class TestRejestr:
    def test_transformacja_jest_dostepna_po_nazwie(self):
        assert get_transform("polregio") is rebuild
