"""Testy dopasowywania współrzędnych peronów.

Dwie rzeczy, w których łatwo o cichy błąd: translacja oznaczenia peronu
(POLREGIO podaje rzymskie, lista PLK arabskie) i normalizacja nazwy stacji
(klucze w liście PLK gubią wielkie litery diakrytyczne, więc nie wolno ich
użyć do wyszukiwania).
"""

from __future__ import annotations

import json

import pytest

from feedsodt.platforms import (
    MIN_STATIONS,
    PlatformError,
    PlatformIndex,
    normalize,
    parse_platform_code,
)


def entry(station: str, platform: str, track: str | None, lon: float, lat: float) -> dict:
    return {
        "station_name": station,
        "operator": "PLK",
        "platform": platform,
        "track": track,
        "location": [lon, lat],
        "exact_location": True,
        "single_track_platform": False,
    }


def index(*entries: dict) -> PlatformIndex:
    payload: dict[str, list[dict]] = {}
    for item in entries:
        payload.setdefault(item["station_name"].lower(), []).append(item)
    return PlatformIndex.from_json(json.dumps(payload))


class TestOznaczeniePeronu:
    @pytest.mark.parametrize(
        "code, expected",
        [
            ("I/1", ("1", "1")),
            ("II/3", ("2", "3")),
            ("III/12", ("3", "12")),
            ("IV/8", ("4", "8")),
            ("V/10", ("5", "10")),
            ("II", ("2", None)),
            ("IIIa/5", ("3a", "5")),
            ("Ia/7K", ("1a", "7")),  # litera po numerze toru to dopisek techniczny
            # Peron bywa podany od razu arabski: POLREGIO pisze rzymskimi, ale
            # Koleje Małopolskie mają „peron 5". Lista PLK trzyma arabskie,
            # więc taki zapis wchodzi bez konwersji.
            ("5", ("5", None)),
            ("1/1", ("1", "1")),
        ],
    )
    def test_rzymskie_na_arabskie(self, code, expected):
        assert parse_platform_code(code) == expected

    @pytest.mark.parametrize("code", ["BUS", "", "   ", "peron 2", "X-Y"])
    def test_co_nie_jest_peronem(self, code):
        """`BUS` to przystanek komunikacji zastępczej — peronu nie ma i nie ma
        go czego szukać na liście PLK."""
        assert parse_platform_code(code) is None


class TestNazwaStacji:
    def test_diakrytyki_sa_skladane(self):
        assert normalize("Bardo Śląskie") == normalize("BARDO SLASKIE")
        assert normalize("Bydgoszcz Łęgnowo") == "bydgoszcz legnowo"

    def test_interpunkcja_nie_przeszkadza(self):
        assert normalize("Blankenberg(Meckl)") == normalize("Blankenberg Meckl")
        assert normalize("Dąbrowa-Łazy") == normalize("Dabrowa Lazy")

    def test_klucz_z_pliku_jest_ignorowany(self):
        """Regresja: slugi w platforms-list.json gubią wielkie Ś/Ł/Ą
        (`Bardo Śląskie` -> `bardo-laskie`), więc dopasowanie po kluczu
        traciło 239 stacji."""
        payload = {"bardo-laskie": [entry("Bardo Śląskie", "1", "1", 16.73, 50.50)]}
        found = PlatformIndex.from_json(json.dumps(payload)).locate("Bardo Śląskie", "I/1")

        assert found is not None
        assert (found.lat, found.lon) == (50.5, 16.73)


class TestSzukanie:
    def test_trafienie_w_peron_i_tor(self):
        listing = index(
            entry("Aleksandrów Kujawski", "1", "1", 18.696398, 52.875926),
            entry("Aleksandrów Kujawski", "2", "5", 18.698291, 52.874679),
        )
        found = listing.locate("Aleksandrów Kujawski", "II/5")

        assert (found.lat, found.lon) == (52.874679, 18.698291)
        assert found.quality == "track"

    def test_bez_toru_bierze_srodek_peronu(self):
        """POLREGIO podaje czasem sam peron (`II`). Środek peronu jest wciąż
        o rząd wielkości lepszy niż punkt stacji."""
        listing = index(
            entry("Stargard", "2", "1", 15.0, 53.0),
            entry("Stargard", "2", "3", 15.2, 53.2),
        )
        found = listing.locate("Stargard", "II")

        assert (found.lat, found.lon) == (53.1, 15.1)
        assert found.quality == "platform"

    def test_niezgodny_tor_spada_do_srodka_peronu(self):
        listing = index(entry("Silnowo", "1", "1", 16.0, 53.0))
        found = listing.locate("Silnowo", "I/9")

        assert found.quality == "platform"
        assert (found.lat, found.lon) == (53.0, 16.0)

    def test_nieznana_stacja_i_nieznany_peron(self):
        listing = index(entry("Hel", "1", "1", 18.8, 54.6))

        assert listing.locate("Berlin Ostkreuz", "I/1") is None
        assert listing.locate("Hel", "IV/2") is None
        assert listing.locate("Hel", "BUS") is None

    def test_perony_bez_wspolrzednych_sa_pomijane(self):
        """61 wpisów w liście PLK ma location: null — OSM zna peron, ale nie ma
        jego obrysu."""
        payload = {
            "brzezno": [
                {**entry("Brzeźno", "1", "1", 0, 0), "location": None},
                entry("Brzeźno", "2", "2", 15.5, 53.5),
            ]
        }
        listing = PlatformIndex.from_json(json.dumps(payload))

        assert listing.locate("Brzeźno", "I/1") is None
        assert listing.locate("Brzeźno", "II/2") is not None


class TestWczytywanie:
    def test_wersjonowana_kopia_jest_kompletna(self):
        """Kopia w repozytorium musi wystarczyć, gdy sieci nie ma."""
        listing = PlatformIndex.load()

        assert len(listing) >= MIN_STATIONS
        assert listing.locate("Gdynia Główna", "II/2") is not None

    def test_pusta_lista_to_blad(self):
        with pytest.raises(PlatformError):
            PlatformIndex.from_json("{}")

    def test_obcieta_lista_z_sieci_nie_nadpisuje_kopii(self, tmp_path):
        """Gdyby GitHub Pages wystawił kadłubek, wolimy wczorajszą kopię."""

        class Response:
            text = json.dumps({"hel": [entry("Hel", "1", "1", 18.8, 54.6)]})

            def raise_for_status(self):
                return None

        class Session:
            def get(self, url, timeout=None):
                return Response()

        cache = tmp_path / "platforms-list.json"
        listing = PlatformIndex.load(cache, Session())

        assert len(listing) >= MIN_STATIONS  # kopia z repozytorium, nie kadłubek
        assert not cache.exists()

    def test_blad_sieci_spada_do_kopii(self, tmp_path):
        class Session:
            def get(self, url, timeout=None):
                raise OSError("sieć padła")

        listing = PlatformIndex.load(tmp_path / "platforms-list.json", Session())

        assert len(listing) >= MIN_STATIONS

    def test_swieza_lista_ląduje_w_cache(self, tmp_path):
        pelna = {
            f"stacja-{number}": [entry(f"Stacja {number}", "1", "1", 19.0, 50.0)]
            for number in range(MIN_STATIONS + 1)
        }

        class Response:
            text = json.dumps(pelna)

            def raise_for_status(self):
                return None

        class Session:
            def get(self, url, timeout=None):
                return Response()

        cache = tmp_path / "nested" / "platforms-list.json"
        listing = PlatformIndex.load(cache, Session())

        assert len(listing) == MIN_STATIONS + 1
        assert cache.is_file()
