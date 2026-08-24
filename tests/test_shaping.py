"""Testy kroku dorabiającego shapes.txt.

Silników routujących tu nie ma — podstawiamy atrapę zamiast
`fetch_trip_geometry`. Testowane jest to, czym ten moduł różni się od
`main.py` gtfs-shaper-makera i na czym stoją gwarancje FeedsODT:
determinizm identyfikatorów, cache (również porażek), pilnowanie pokrycia
i strumieniowy zapis, który nie rusza pozostałych tabel.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from conftest import read_gtfs, write_gtfs
from feedsodt import shaping
from feedsodt.area import Area
from feedsodt.shaping import ShapeConfig, ShapeError, parse_modes
from feedsodt.shaping.cache import RETRY_AFTER_DAYS, ShapeCache

STOPS = [
    {"stop_id": "A", "stop_name": "A", "stop_lat": "50.00", "stop_lon": "19.00"},
    {"stop_id": "B", "stop_name": "B", "stop_lat": "50.10", "stop_lon": "19.10"},
    {"stop_id": "C", "stop_name": "C", "stop_lat": "50.20", "stop_lon": "19.20"},
    {"stop_id": "D", "stop_name": "D", "stop_lat": "50.30", "stop_lon": "19.30"},
]


def feed(tmp_path, kursy: list[tuple[str, str, list[str]]], stops=None):
    """`kursy` to lista (trip_id, route_id, sekwencja stop_id).
    route_id `RAIL` ma route_type=2, `BUS` — 3."""
    routes = [
        {"route_id": "RAIL", "route_short_name": "R", "route_type": "2", "agency_id": "PR"},
        {"route_id": "BUS", "route_short_name": "BUS", "route_type": "3", "agency_id": "PR"},
    ]
    trips, stop_times = [], []
    for trip_id, route_id, sekwencja in kursy:
        trips.append(
            {"route_id": route_id, "service_id": "S1", "trip_id": trip_id, "trip_headsign": "X"}
        )
        for order, stop in enumerate(sekwencja, start=1):
            stop_times.append(
                {
                    "trip_id": trip_id,
                    "arrival_time": f"{7 + order:02d}:00:00",
                    "departure_time": f"{7 + order:02d}:00:00",
                    "stop_id": stop,
                    "stop_sequence": str(order),
                }
            )
    return write_gtfs(
        tmp_path / "feed.zip",
        {
            "agency.txt": [
                {
                    "agency_id": "PR",
                    "agency_name": "PR",
                    "agency_url": "https://example.pl",
                    "agency_timezone": "Europe/Warsaw",
                }
            ],
            "stops.txt": stops or STOPS,
            "routes.txt": routes,
            "trips.txt": trips,
            "stop_times.txt": stop_times,
            "calendar.txt": [
                {"service_id": "S1", "start_date": "20260302", "end_date": "20261212"}
            ],
        },
    )


class Router:
    """Atrapa silnika: zwraca prostą łamaną przez podane punkty i liczy wywołania.

    `unroutable` to zbiór indeksów punktu początkowego, dla których udaje
    porażkę — tak jak silnik odpowiada na kursy wychodzące poza wycinek OSM.
    """

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.calls = 0
        self.fail_for = fail_for or set()

    def __call__(self, client, points):
        self.calls += 1
        if any(f"{lat:.2f}" in self.fail_for for lat, _ in points):
            return None, "skipped"
        # Punkty pośrednie dokładamy, żeby RDP miał co upraszczać.
        coords = []
        for lat, lon in points:
            coords.append([lon, lat])
            coords.append([lon + 0.001, lat + 0.001])
        return coords, "match"


@pytest.fixture
def router(monkeypatch):
    stub = Router()
    monkeypatch.setattr(shaping, "fetch_trip_geometry", stub)
    monkeypatch.setattr(shaping, "_check_engines", lambda clients, sequences: None)
    return stub


#: Kwadrat obejmujący przystanki A–D; E leży poza nim.
OBSZAR = Area.from_geometry(
    "test", {"type": "Polygon", "coordinates": [[[18.9, 49.9], [19.4, 49.9], [19.4, 50.4], [18.9, 50.4], [18.9, 49.9]]]}
)


def config(**kwargs) -> ShapeConfig:
    return ShapeConfig(
        modes=kwargs.pop("modes", parse_modes("rail,bus")),
        rail_url="http://rail.invalid",
        bus_url="http://bus.invalid",
        workers=2,
        **kwargs,
    )


class TestTryby:
    def test_lista_trybow(self):
        assert parse_modes("rail, bus") == frozenset({"rail", "bus"})
        assert parse_modes("") == frozenset()

    def test_nieznany_tryb_to_czytelny_blad(self):
        with pytest.raises(ShapeError, match="nieznany tryb"):
            parse_modes("rail,metro")

    def test_kształtowany_jest_tylko_wybrany_tryb(self, tmp_path, router):
        source = feed(
            tmp_path, [("T1", "RAIL", ["A", "B", "C"]), ("T2", "BUS", ["A", "B", "C"])]
        )
        dest = tmp_path / "out.zip"
        shaping.shape(source, dest, config(modes=parse_modes("rail")))

        shape_ids = {t["trip_id"]: t["shape_id"] for t in read_gtfs(dest, "trips.txt")}

        assert shape_ids["T1"]
        assert shape_ids["T2"] == ""


class TestDeterminizm:
    def test_ten_sam_feed_daje_ten_sam_plik(self, tmp_path, router):
        """Regresja na główną różnicę wobec main.py gtfs-shaper-makera:
        tam shape_id nadaje kolejność zakończenia wątków, więc sha256 zmieniało
        się przy każdym przebiegu i feed byłby republikowany codziennie."""
        source = feed(
            tmp_path,
            [(f"T{n}", "RAIL", ["A", "B", "C", "D"][: 2 + n % 3]) for n in range(1, 12)],
        )
        first, second = tmp_path / "a.zip", tmp_path / "b.zip"
        shaping.shape(source, first, config())
        shaping.shape(source, second, config())

        assert first.read_bytes() == second.read_bytes()

    def test_shape_id_zalezy_od_sekwencji_a_nie_od_kolejnosci(self, tmp_path, router):
        source = feed(tmp_path, [("T1", "RAIL", ["A", "B", "C"])])
        dest = tmp_path / "out.zip"
        shaping.shape(source, dest, config())

        assert read_gtfs(dest, "trips.txt")[0]["shape_id"] == shaping._shape_id(
            "rail", ("A", "B", "C")
        )

    def test_jeden_kształt_na_wspolna_sekwencje(self, tmp_path, router):
        """Kursy jadące tą samą trasą dzielą kształt — inaczej shapes.txt
        puchnie 9-krotnie (39766 kursów wobec 4497 sekwencji)."""
        source = feed(
            tmp_path,
            [("T1", "RAIL", ["A", "B", "C"]), ("T2", "RAIL", ["A", "B", "C"])],
        )
        dest = tmp_path / "out.zip"
        shaping.shape(source, dest, config())

        shape_ids = {t["shape_id"] for t in read_gtfs(dest, "trips.txt")}

        assert len(shape_ids) == 1
        assert router.calls == 1


class TestCache:
    def test_drugi_przebieg_nie_rusza_silnika(self, tmp_path, router):
        source = feed(tmp_path, [("T1", "RAIL", ["A", "B", "C"])])
        cache = tmp_path / "shapes.sqlite"

        shaping.shape(source, tmp_path / "a.zip", config(cache_path=cache))
        po_pierwszym = router.calls
        stats = shaping.shape(source, tmp_path / "b.zip", config(cache_path=cache))

        assert po_pierwszym == 1
        assert router.calls == 1
        assert stats.from_cache == 1

    def test_wynik_z_cache_jest_identyczny_z_policzonym(self, tmp_path, router):
        source = feed(tmp_path, [("T1", "RAIL", ["A", "B", "C", "D"])])
        cache = tmp_path / "shapes.sqlite"
        first, second = tmp_path / "a.zip", tmp_path / "b.zip"

        shaping.shape(source, first, config(cache_path=cache))
        shaping.shape(source, second, config(cache_path=cache))

        assert first.read_bytes() == second.read_bytes()

    def test_zmiana_wspolrzednych_uniewaznia_kształt(self, tmp_path, router):
        """Klucz cache'a to współrzędne, więc poprawka położenia peronu ma
        przeliczyć dokładnie te kształty, które przez niego prowadzą."""
        cache = tmp_path / "shapes.sqlite"
        source = feed(tmp_path, [("T1", "RAIL", ["A", "B", "C"])])
        shaping.shape(source, tmp_path / "a.zip", config(cache_path=cache))

        przesunięte = [
            {**stop, "stop_lat": "50.55"} if stop["stop_id"] == "B" else stop for stop in STOPS
        ]
        source = feed(tmp_path, [("T1", "RAIL", ["A", "B", "C"])], stops=przesunięte)
        stats = shaping.shape(source, tmp_path / "b.zip", config(cache_path=cache))

        assert stats.from_cache == 0
        assert router.calls == 2

    def test_porazka_nie_jest_ponawiana_nastepnej_nocy(self, tmp_path, monkeypatch):
        """39 sekwencji POLREGIO wychodzi poza wycinek OSM. Każda próba to
        kaskada trzech zapytań czekających na timeout — minuta na przebieg."""
        stub = Router(fail_for={"50.00"})
        monkeypatch.setattr(shaping, "fetch_trip_geometry", stub)
        monkeypatch.setattr(shaping, "_check_engines", lambda clients, sequences: None)

        source = feed(
            tmp_path, [("T1", "RAIL", ["A", "B", "C"]), ("T2", "RAIL", ["B", "C", "D"])]
        )
        cache = tmp_path / "shapes.sqlite"
        shaping.shape(source, tmp_path / "a.zip", config(cache_path=cache))
        po_pierwszym = stub.calls
        stats = shaping.shape(source, tmp_path / "b.zip", config(cache_path=cache))

        assert po_pierwszym == 2
        assert stats.known_bad == 1
        assert stub.calls == 2  # nieudanej sekwencji nie tknięto ponownie

    def test_porazka_przeterminowuje_sie(self, tmp_path):
        """Podmiana wycinka OSM na szerszy ma się w końcu odbić w wyniku bez
        ręcznego czyszczenia cache'a."""
        cache_path = tmp_path / "shapes.sqlite"
        dawno = date.today() - timedelta(days=RETRY_AFTER_DAYS + 1)

        with ShapeCache.open(cache_path, today=dawno) as cache:
            cache.put_failure("k", "skipped")
            cache.commit()

        with ShapeCache.open(cache_path) as cache:
            assert cache.lookup("k").known_bad is False

    def test_niezapisywalny_cache_nie_wywraca_przebiegu(self, tmp_path, router):
        source = feed(tmp_path, [("T1", "RAIL", ["A", "B", "C"])])
        zajete = tmp_path / "plik"
        zajete.write_text("to nie jest baza")
        dest = tmp_path / "out.zip"

        stats = shaping.shape(source, dest, config(cache_path=zajete / "shapes.sqlite"))

        assert stats.shaped == 1


class TestPokrycie:
    def test_nieudana_sekwencja_zostawia_kurs_bez_kształtu(self, tmp_path, monkeypatch):
        stub = Router(fail_for={"50.30"})
        monkeypatch.setattr(shaping, "fetch_trip_geometry", stub)
        monkeypatch.setattr(shaping, "_check_engines", lambda clients, sequences: None)

        source = feed(
            tmp_path, [("T1", "RAIL", ["A", "B", "C"]), ("T2", "RAIL", ["B", "C", "D"])]
        )
        dest = tmp_path / "out.zip"
        shaping.shape(source, dest, config())

        shape_ids = {t["trip_id"]: t["shape_id"] for t in read_gtfs(dest, "trips.txt")}

        assert shape_ids["T1"]
        assert shape_ids["T2"] == ""

    def test_zbyt_niskie_pokrycie_przerywa_publikacje(self, tmp_path, monkeypatch):
        """Feed, w którym połowa kursów straciła geometrię, jest gorszy niż
        wczorajszy komplet — lepiej zostawić poprzednią wersję."""
        stub = Router(fail_for={"50.00", "50.10"})
        monkeypatch.setattr(shaping, "fetch_trip_geometry", stub)
        monkeypatch.setattr(shaping, "_check_engines", lambda clients, sequences: None)

        source = feed(
            tmp_path, [("T1", "RAIL", ["A", "B", "C"]), ("T2", "RAIL", ["B", "C", "D"])]
        )

        with pytest.raises(ShapeError, match="pokrywa|kursów"):
            shaping.shape(source, tmp_path / "out.zip", config())

    def test_kurs_o_jednym_przystanku_jest_pomijany(self, tmp_path, router):
        source = feed(
            tmp_path,
            [("T1", "RAIL", ["A", "B", "C"])] + [(f"T{n}", "RAIL", ["A"]) for n in range(2, 5)],
        )
        dest = tmp_path / "out.zip"
        shaping.shape(source, dest, config())

        shape_ids = {t["trip_id"]: t["shape_id"] for t in read_gtfs(dest, "trips.txt")}

        assert shape_ids["T1"]
        assert shape_ids["T2"] == ""

    def test_niedostepny_silnik_mowi_wprost_co_zrobic(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shaping, "fetch_trip_geometry", Router())
        source = feed(tmp_path, [("T1", "RAIL", ["A", "B", "C"])])

        with pytest.raises(ShapeError, match="docker compose"):
            shaping.shape(source, tmp_path / "out.zip", config())


class TestZapis:
    def test_pozostale_tabele_ida_bajt_w_bajt(self, tmp_path, router):
        import zipfile

        source = feed(tmp_path, [("T1", "RAIL", ["A", "B", "C"])])
        dest = tmp_path / "out.zip"
        shaping.shape(source, dest, config())

        with zipfile.ZipFile(source) as a, zipfile.ZipFile(dest) as b:
            for name in ("stop_times.txt", "stops.txt", "routes.txt", "calendar.txt"):
                assert a.read(name) == b.read(name), name

    def test_shapes_i_trips_trzymaja_sie_wzajemnie(self, tmp_path, router):
        source = feed(
            tmp_path, [("T1", "RAIL", ["A", "B", "C"]), ("T2", "BUS", ["B", "C", "D"])]
        )
        dest = tmp_path / "out.zip"
        shaping.shape(source, dest, config())

        w_shapes = {row["shape_id"] for row in read_gtfs(dest, "shapes.txt")}
        w_trips = {t["shape_id"] for t in read_gtfs(dest, "trips.txt") if t["shape_id"]}

        assert w_trips == w_shapes

    def test_kolejnosc_punktow_jest_ciagla(self, tmp_path, router):
        source = feed(tmp_path, [("T1", "RAIL", ["A", "B", "C", "D"])])
        dest = tmp_path / "out.zip"
        shaping.shape(source, dest, config())

        kolejnosc = [int(row["shape_pt_sequence"]) for row in read_gtfs(dest, "shapes.txt")]

        assert kolejnosc == list(range(len(kolejnosc)))

    def test_brak_wymaganej_tabeli_to_czytelny_blad(self, tmp_path, router):
        niepelny = write_gtfs(tmp_path / "zle.zip", {"agency.txt": [{"agency_id": "PR"}]})

        with pytest.raises(ShapeError, match="brakuje"):
            shaping.shape(niepelny, tmp_path / "out.zip", config())


class TestUpraszczanie:
    def test_punkty_na_prostej_wypadaja(self):
        prosta = [[19.0 + step * 0.001, 50.0] for step in range(40)]

        assert shaping._simplify(prosta) == [prosta[0], prosta[-1]]

    def test_zakret_zostaje(self):
        łamana = [[19.0, 50.0], [19.1, 50.0], [19.1, 50.1]]

        assert shaping._simplify(łamana) == łamana


class TestObszar:
    """Poza wycinkiem OSM router nie odmawia — dosuwa przystanek do
    najbliższego miejsca, które zna, i zwraca linię biegnącą gdzie indziej.
    Kurs, który wyjeżdża za granicę, ma zostać bez kształtu."""

    ZAGRANICA = [
        {"stop_id": "E", "stop_name": "Zagranica", "stop_lat": "50.50", "stop_lon": "13.50"}
    ]

    def test_kurs_wyjezdzajacy_poza_obszar_nie_dostaje_ksztaltu(self, tmp_path, router):
        source = feed(
            tmp_path,
            [("T1", "RAIL", ["A", "B", "C"]), ("T2", "RAIL", ["B", "C", "E"])],
            stops=STOPS + self.ZAGRANICA,
        )
        dest = tmp_path / "out.zip"
        stats = shaping.shape(source, dest, config(area=OBSZAR))

        shape_ids = {t["trip_id"]: t["shape_id"] for t in read_gtfs(dest, "trips.txt")}

        assert shape_ids["T1"]
        assert shape_ids["T2"] == ""
        assert stats.trips_outside_area == 1

    def test_taki_kurs_nie_jest_nawet_routowany(self, tmp_path, router):
        """Nie ma po co pytać silnika o coś, czego i tak nie przyjmiemy."""
        source = feed(
            tmp_path, [("T1", "RAIL", ["B", "C", "E"])], stops=STOPS + self.ZAGRANICA
        )

        with pytest.raises(ShapeError, match="nie ma czego kształtować"):
            shaping.shape(source, tmp_path / "out.zip", config(area=OBSZAR))
        assert router.calls == 0

    def test_kursy_poza_obszarem_nie_psuja_pokrycia(self, tmp_path, router):
        """Trzy kursy za granicą i jeden w kraju to nie jest 25% pokrycia —
        tamtych trzech nie da się zakształtować z założenia."""
        source = feed(
            tmp_path,
            [("T1", "RAIL", ["A", "B", "C"])]
            + [(f"T{n}", "RAIL", ["B", "C", "E"]) for n in range(2, 5)],
            stops=STOPS + self.ZAGRANICA,
        )
        stats = shaping.shape(source, tmp_path / "out.zip", config(area=OBSZAR))

        assert stats.coverage == 1.0
        assert stats.trips_outside_area == 3

    def test_bez_obszaru_nic_nie_jest_filtrowane(self, tmp_path, router):
        source = feed(
            tmp_path, [("T1", "RAIL", ["B", "C", "E"])], stops=STOPS + self.ZAGRANICA
        )
        dest = tmp_path / "out.zip"
        stats = shaping.shape(source, dest, config())

        assert stats.trips_outside_area == 0
        assert read_gtfs(dest, "trips.txt")[0]["shape_id"]


class Bladzacy:
    """Atrapa silnika zwracająca linię oddaloną od przystanków — tak zachowuje
    się router, który nie potrafi dopasować sekwencji, ale i tak coś zwraca."""

    def __init__(self, offset_deg: float) -> None:
        self.offset = offset_deg
        self.calls = 0

    def __call__(self, client, points):
        self.calls += 1
        return [[lon + self.offset, lat + self.offset] for lat, lon in points], "route"


class TestOdchylenie:
    def test_linia_daleko_od_przystankow_jest_odrzucana(self, tmp_path, monkeypatch):
        stub = Bladzacy(0.2)  # ok. 20 km w bok
        monkeypatch.setattr(shaping, "fetch_trip_geometry", stub)
        monkeypatch.setattr(shaping, "_check_engines", lambda clients, sequences: None)

        source = feed(tmp_path, [("T1", "RAIL", ["A", "B", "C"])])

        with pytest.raises(ShapeError, match="pokrywa|kursów"):
            shaping.shape(source, tmp_path / "out.zip", config())

    def test_niewielkie_odchylenie_jest_akceptowane(self, tmp_path, monkeypatch):
        """Peron stoi obok osi toru, więc kilkadziesiąt metrów to norma."""
        stub = Bladzacy(0.0009)  # ok. 100 m
        monkeypatch.setattr(shaping, "fetch_trip_geometry", stub)
        monkeypatch.setattr(shaping, "_check_engines", lambda clients, sequences: None)

        source = feed(tmp_path, [("T1", "RAIL", ["A", "B", "C"])])
        stats = shaping.shape(source, tmp_path / "out.zip", config())

        assert stats.shaped == 1
        assert stats.rejected == 0

    def test_odrzucony_ksztalt_nie_jest_liczony_od_nowa(self, tmp_path, monkeypatch):
        stub = Bladzacy(0.2)
        monkeypatch.setattr(shaping, "fetch_trip_geometry", stub)
        monkeypatch.setattr(shaping, "_check_engines", lambda clients, sequences: None)
        cache = tmp_path / "shapes.sqlite"
        source = feed(
            tmp_path, [("T1", "RAIL", ["A", "B", "C"]), ("T2", "RAIL", ["B", "C", "D"])]
        )

        for _ in range(2):
            with pytest.raises(ShapeError):
                shaping.shape(source, tmp_path / "out.zip", config(cache_path=cache))

        assert stub.calls == 2  # dwie sekwencje policzone raz, nie dwa razy

    def test_odleglosc_liczy_sie_do_odcinka_nie_do_wierzcholka(self):
        """Po uproszczeniu RDP prosty tor ma dwa punkty, więc przystanek w jego
        połowie bywa kilometry od najbliższego wierzchołka, leżąc na linii."""
        linia = [[19.0, 50.0], [19.0, 51.0]]
        srodek = ((50.5, 19.0),)

        assert shaping._max_deviation(srodek, linia) < 1.0


class TestBrakWspolrzednych:
    """Przystanek zapisany jako 0,0 to brakująca współrzędna, a nie punkt
    w zatoce Gwinejskiej. Router dostałby cel oddalony o 5,5 tys. km i zwrócił
    dla całego kursu geometrię bez związku z rzeczywistością."""

    ZEROWY = [
        {"stop_id": "Z", "stop_name": "BAŃSKA NIŻNA", "stop_lat": "0.000000", "stop_lon": "0.000000"}
    ]

    def test_kurs_przez_taki_przystanek_nie_dostaje_ksztaltu(self, tmp_path, router):
        source = feed(
            tmp_path,
            [("T1", "RAIL", ["A", "B", "C"]), ("T2", "RAIL", ["B", "C", "Z"])],
            stops=STOPS + self.ZEROWY,
        )
        dest = tmp_path / "out.zip"
        stats = shaping.shape(source, dest, config())

        shape_ids = {t["trip_id"]: t["shape_id"] for t in read_gtfs(dest, "trips.txt")}

        assert shape_ids["T1"]
        assert shape_ids["T2"] == ""
        assert stats.stops_without_coords == ["BAŃSKA NIŻNA"]

    def test_nie_jest_mylony_z_wyjazdem_za_granice(self, tmp_path, router):
        """Oba przypadki dają brak geometrii, ale z zupełnie różnych powodów —
        i log ma o tym mówić wprost."""
        source = feed(
            tmp_path, [("T1", "RAIL", ["A", "B", "C"]), ("T2", "RAIL", ["B", "C", "Z"])],
            stops=STOPS + self.ZEROWY,
        )
        stats = shaping.shape(source, tmp_path / "out.zip", config(area=OBSZAR))

        assert stats.trips_outside_area == 0
        assert stats.stops_without_coords == ["BAŃSKA NIŻNA"]

    def test_zdrowy_przystanek_na_zerowym_poludniku_nie_wypada(self, tmp_path, router):
        """Odrzucamy dokładnie (0, 0), a nie samo zero w jednej ze współrzędnych."""
        greenwich = [
            {"stop_id": "G", "stop_name": "Greenwich", "stop_lat": "51.48", "stop_lon": "0.000000"}
        ]
        source = feed(
            tmp_path, [("T1", "RAIL", ["A", "B", "G"])], stops=STOPS + greenwich
        )
        stats = shaping.shape(source, tmp_path / "out.zip", config())

        assert stats.stops_without_coords == []
        assert stats.shaped == 1
