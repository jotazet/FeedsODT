"""Testy przebudowy feedu SKA (Koleje Małopolskie).

Sedno: przewoźnik trzyma numer peronu w `stop_headsign` — polu na kierunkowskaz
widoczny przy zatrzymaniu, nie na numer peronu. Skutkiem są trzy rzeczy naraz:
planer pokazuje pasażerowi „peron I, tor 9" jako kierunek jazdy, wszystkie
perony stacji dzielą jeden punkt na mapie, a peronu nie da się znaleźć tam,
gdzie konsument go szuka — w `stops.txt`.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from conftest import read_gtfs, write_gtfs
from feedsodt.transform import Context, TransformError, get_transform
from feedsodt.transform.ska import platform_code, rebuild

#: Prawdziwe nazwy stacji — współrzędne peronów wyszukujemy po nazwie
#: w wersjonowanej liście PLK (feedsodt/data/platforms-list.json).
STOPS = [
    {
        "stop_id": "80416",
        "stop_code": "",
        "stop_name": "KRAKÓW GŁÓWNY",
        "stop_lat": "50.0683947",
        "stop_lon": "19.9475035",
        "location_type": "",
        "parent_station": "",
    },
    {
        "stop_id": "80002",
        "stop_code": "",
        "stop_name": "KRAKÓW ŁAGIEWNIKI",
        "stop_lat": "50.0202",
        "stop_lon": "19.9276",
        "location_type": "",
        "parent_station": "",
    },
    {
        "stop_id": "80036",
        "stop_code": "",
        "stop_name": "SKAWINA",
        "stop_lat": "49.97714",
        "stop_lon": "19.8218725",
        "location_type": "",
        "parent_station": "",
    },
]


def feed(tmp_path: Path, kursy, stops=None) -> Path:
    """`kursy` to lista (trip_id, [(stop_id, opis peronu), ...])."""
    trips, times = [], []
    for trip_id, sekwencja in kursy:
        trips.append(
            {
                "route_id": "KML",
                "service_id": "S1",
                "trip_id": trip_id,
                "trip_headsign": "",
                "trip_short_name": f"{trip_id} KML",
                "direction_id": "",
                "shape_id": "",
            }
        )
        for order, (stop_id, peron) in enumerate(sekwencja):
            times.append(
                {
                    "trip_id": trip_id,
                    "arrival_time": f"{6 + order:02d}:12:00",
                    "departure_time": f"{6 + order:02d}:12:00",
                    "stop_id": stop_id,
                    "stop_sequence": str(order),
                    "stop_headsign": peron,
                    "pickup_type": "",
                    "drop_off_type": "",
                    "shape_dist_traveled": f"{order * 1.244:.3f}",
                }
            )
    return write_gtfs(
        tmp_path / "ska.zip",
        {
            "agency.txt": [
                {
                    "agency_id": "1",
                    "agency_name": "Koleje Małopolskie sp. z o.o.",
                    "agency_url": "https://kolejemalopolskie.com.pl/",
                    "agency_timezone": "Europe/Warsaw",
                }
            ],
            "stops.txt": stops or STOPS,
            "routes.txt": [
                {
                    "route_id": "KML",
                    "agency_id": "1",
                    "route_short_name": "",
                    "route_long_name": "KML",
                    "route_type": "2",
                }
            ],
            "trips.txt": trips,
            "stop_times.txt": times,
            "calendar_dates.txt": [
                {"service_id": "S1", "date": "20260813", "exception_type": "1"},
                {"service_id": "S1", "date": "20261024", "exception_type": "1"},
            ],
        },
    )


@pytest.fixture
def przebudowany(tmp_path):
    source = feed(
        tmp_path,
        [
            ("T1", [("80416", "peron I, tor 9"), ("80002", "peron I, tor 1"), ("80036", "peron II, tor 2")]),
            ("T2", [("80036", "peron I, tor 1"), ("80002", "peron II, tor 2"), ("80416", "peron III, tor 1")]),
        ],
    )
    dest = tmp_path / "fixed.zip"
    rebuild(source, dest, Context())
    return dest


class TestOpisPeronu:
    @pytest.mark.parametrize(
        "headsign, expected",
        [
            ("peron I, tor 9", "I/9"),
            ("peron II, tor 2", "II/2"),
            ("peron IV, tor 103", "IV/103"),
            ("peron BUS", "BUS"),
            ("peron 5", "5"),
            ("", ""),
            ("do Zakopanego", ""),
        ],
    )
    def test_zamiana_na_platform_code(self, headsign, expected):
        assert platform_code(headsign) == expected


class TestPerony:
    def test_kazdy_peron_to_osobny_przystanek(self, przebudowany):
        stops = read_gtfs(przebudowany, "stops.txt")
        krakow = [s for s in stops if s["stop_name"] == "Kraków Główny"]

        assert {s["platform_code"] for s in krakow} == {"", "I/9", "III/1"}
        assert len([s for s in krakow if s["location_type"] == "0"]) == 2

    def test_perony_maja_rozne_wspolrzedne(self, przebudowany):
        """W oryginale wszystkie perony stacji dzielą jeden punkt."""
        stops = read_gtfs(przebudowany, "stops.txt")
        punkty = {
            (s["stop_lat"], s["stop_lon"])
            for s in stops
            if s["stop_name"] == "Kraków Główny" and s["location_type"] == "0"
        }

        assert len(punkty) == 2

    def test_perony_wisza_pod_stacja(self, przebudowany):
        stops = read_gtfs(przebudowany, "stops.txt")
        stacje = {s["stop_id"] for s in stops if s["location_type"] == "1"}
        perony = [s for s in stops if s["platform_code"]]

        assert stacje == {"80416", "80002", "80036"}
        assert all(s["parent_station"] in stacje for s in perony)
        assert all(s["location_type"] == "0" for s in perony)

    def test_stacja_o_jednym_peronie_nie_dostaje_wezla(self, tmp_path):
        source = feed(tmp_path, [("T1", [("80002", "peron I, tor 1"), ("80036", "peron I, tor 1")])])
        dest = tmp_path / "fixed.zip"
        rebuild(source, dest, Context())

        stops = read_gtfs(dest, "stops.txt")

        assert [s for s in stops if s["location_type"] == "1"] == []
        assert {s["stop_id"] for s in stops} == {"80002", "80036"}

    def test_nieuzywane_przystanki_wypadaja(self, tmp_path):
        """Przystanek, na którym nic nie staje, to pinezka bez pokrycia."""
        source = feed(tmp_path, [("T1", [("80002", "peron I, tor 1"), ("80036", "peron I, tor 1")])])
        dest = tmp_path / "fixed.zip"
        stats = rebuild(source, dest, Context())

        assert stats.stops_dropped == 1
        assert "Kraków Główny" not in {s["stop_name"] for s in read_gtfs(dest, "stops.txt")}

    def test_stop_times_wskazuje_peron_a_nie_stacje(self, przebudowany):
        stops = {s["stop_id"]: s for s in read_gtfs(przebudowany, "stops.txt")}

        for row in read_gtfs(przebudowany, "stop_times.txt"):
            assert stops[row["stop_id"]]["location_type"] == "0"

    def test_stop_headsign_znika(self, przebudowany):
        """To nie był kierunkowskaz, tylko numer peronu — a peron ma teraz
        własne miejsce w stops.txt."""
        assert "stop_headsign" not in read_gtfs(przebudowany, "stop_times.txt")[0]

    def test_shape_dist_traveled_znika(self, przebudowany):
        """W oryginale wypełnione w kilometrach przy pustym shapes.txt.
        Geometrię dorabiamy sami, więc te odległości byłyby z nią sprzeczne."""
        assert "shape_dist_traveled" not in read_gtfs(przebudowany, "stop_times.txt")[0]

    def test_peron_bus_zostaje_przy_punkcie_stacji(self, tmp_path):
        """`peron BUS` to komunikacja zastępcza — peronu nie ma i nie ma go
        czego szukać na liście PLK."""
        source = feed(
            tmp_path,
            [("T1", [("80416", "peron BUS"), ("80002", "peron I, tor 1"), ("80036", "peron I, tor 1")])],
        )
        dest = tmp_path / "fixed.zip"
        rebuild(source, dest, Context())

        bus = [s for s in read_gtfs(dest, "stops.txt") if s["platform_code"] == "BUS"][0]

        assert (bus["stop_lat"], bus["stop_lon"]) == ("50.0683947", "19.9475035")


class TestNaprawyZListyPLK:
    def test_wielkosc_liter_w_nazwach(self, przebudowany):
        """Feed pisze stacje wersalikami; lista PLK trzyma poprawny zapis."""
        nazwy = {s["stop_name"] for s in read_gtfs(przebudowany, "stops.txt")}

        assert "Kraków Główny" in nazwy
        assert "KRAKÓW GŁÓWNY" not in nazwy

    @pytest.mark.parametrize("zero", ["0", "0.0", "0.000000"])
    def test_wspolrzedne_0_0_sa_naprawiane(self, tmp_path, zero):
        """BAŃSKA NIŻNA ma w źródle 0,0 — punkt w zatoce Gwinejskiej,
        5,5 tys. km od Podhala."""
        stops = STOPS + [
            {
                "stop_id": "265252",
                "stop_code": "",
                "stop_name": "BAŃSKA NIŻNA",
                # Tak to wygląda w prawdziwym feedzie — samo "0", nie "0.000000".
                "stop_lat": zero,
                "stop_lon": zero,
                "location_type": "",
                "parent_station": "",
            }
        ]
        source = feed(
            tmp_path,
            [("T1", [("80002", "peron I, tor 1"), ("265252", "peron I, tor 1"), ("80036", "peron I, tor 1")])],
            stops=stops,
        )
        dest = tmp_path / "fixed.zip"
        stats = rebuild(source, dest, Context())

        banska = [s for s in read_gtfs(dest, "stops.txt") if s["stop_name"] == "Bańska Niżna"][0]

        assert stats.fixed_null_island == ["Bańska Niżna"]
        assert 49.3 < float(banska["stop_lat"]) < 49.5
        assert 19.9 < float(banska["stop_lon"]) < 20.1

    def test_stacja_spoza_listy_bez_wspolrzednych_to_blad(self, tmp_path):
        """Lepiej zostawić poprzednią wersję feedu niż opublikować przystanek
        na Wyspie Zerowej."""
        stops = STOPS + [
            {
                "stop_id": "X",
                "stop_code": "",
                "stop_name": "PLAVEC",
                "stop_lat": "0",
                "stop_lon": "0",
                "location_type": "",
                "parent_station": "",
            }
        ]
        source = feed(tmp_path, [("T1", [("80002", "peron I, tor 1"), ("X", "peron I, tor 1")])], stops=stops)

        with pytest.raises(TransformError, match="PLAVEC"):
            rebuild(source, tmp_path / "fixed.zip", Context())


class TestKursy:
    def test_kierunkowskaz_to_stacja_docelowa(self, przebudowany):
        """W oryginale trip_headsign jest pusty we wszystkich 1224 kursach."""
        headsigny = {t["trip_id"]: t["trip_headsign"] for t in read_gtfs(przebudowany, "trips.txt")}

        assert headsigny == {"T1": "Skawina", "T2": "Kraków Główny"}

    def test_kierunek_jazdy(self, przebudowany):
        kierunki = {t["trip_id"]: t["direction_id"] for t in read_gtfs(przebudowany, "trips.txt")}

        assert sorted(kierunki.values()) == ["0", "1"]


class TestIntegralnosc:
    def test_pozostale_tabele_ida_bajt_w_bajt(self, tmp_path, przebudowany):
        source = tmp_path / "ska.zip"
        with zipfile.ZipFile(source) as a, zipfile.ZipFile(przebudowany) as b:
            for name in ("agency.txt", "routes.txt", "calendar_dates.txt"):
                assert a.read(name) == b.read(name), name

    def test_determinizm(self, tmp_path):
        source = feed(tmp_path, [("T1", [("80416", "peron I, tor 9"), ("80036", "peron I, tor 1")])])
        first, second = tmp_path / "a.zip", tmp_path / "b.zip"
        rebuild(source, first, Context())
        rebuild(source, second, Context())

        assert first.read_bytes() == second.read_bytes()

    def test_brak_wymaganej_tabeli_to_czytelny_blad(self, tmp_path):
        niepelny = write_gtfs(tmp_path / "zle.zip", {"agency.txt": [{"agency_id": "1"}]})

        with pytest.raises(TransformError, match="brakuje"):
            rebuild(niepelny, tmp_path / "fixed.zip", Context())


class TestRejestr:
    def test_transformacja_jest_dostepna_po_nazwie(self):
        assert get_transform("ska") is rebuild
