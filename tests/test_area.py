"""Testy obszaru, poza którym nie liczymy geometrii.

Reguła ma sens tylko wtedy, gdy potrafi rozdzielić stacje po dwóch stronach
rzeki granicznej — Zgorzelec od Görlitz dzielą 2 km. Stąd oprócz testów na
kwadratach jest test na wersjonowanej granicy Polski.
"""

from __future__ import annotations

import pytest

from feedsodt.area import Area, AreaError

#: Kwadrat 0..10 x 0..10 z dziurą 4..6 x 4..6.
KWADRAT = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
    ],
}


class TestWielokat:
    @pytest.fixture
    def obszar(self):
        return Area.from_geometry("kwadrat", KWADRAT)

    def test_wnetrze_i_zewnetrze(self, obszar):
        assert obszar.contains(5, 1) is True
        assert obszar.contains(5, 20) is False
        assert obszar.contains(-1, 5) is False

    def test_dziura_nie_nalezy_do_obszaru(self, obszar):
        assert obszar.contains(5, 5) is False
        assert obszar.contains(3, 3) is True

    def test_bbox_odsiewa_bez_liczenia(self, obszar):
        assert obszar.bbox == (0, 0, 10, 10)
        assert obszar.contains(1000, 1000) is False

    def test_multipolygon(self):
        obszar = Area.from_geometry(
            "wyspy",
            {
                "type": "MultiPolygon",
                "coordinates": [
                    [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                    [[[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]],
                ],
            },
        )

        assert obszar.contains(0.5, 0.5) is True
        assert obszar.contains(5.5, 5.5) is True
        assert obszar.contains(3, 3) is False

    def test_zla_geometria_to_czytelny_blad(self):
        with pytest.raises(AreaError, match="Polygon"):
            Area.from_geometry("punkt", {"type": "Point", "coordinates": [1, 2]})

    def test_nieznany_obszar_wymienia_dostepne(self):
        with pytest.raises(AreaError, match="poland"):
            Area.load("atlantyda")


class TestGranicaPolski:
    """Wersjonowana granica z OSM — bez niej reguła `shape_area` jest ślepa."""

    @staticmethod
    @pytest.fixture(scope="class")
    def polska():
        return Area.load("poland")

    @pytest.mark.parametrize(
        "nazwa, lat, lon, w_polsce",
        [
            # Pary po dwóch stronach rzeki granicznej — najtrudniejszy przypadek.
            ("Zgorzelec", 51.1467, 15.0080, True),
            ("Görlitz", 51.1479, 14.9797, False),
            ("Gubin", 51.9490, 14.7480, True),
            ("Guben", 51.9530, 14.7150, False),
            ("Słubice", 52.3480, 14.5730, True),
            ("Frankfurt/Oder", 52.3390, 14.5460, False),
            # Stacje, które POLREGIO obsługuje w głębi Niemiec.
            ("Berlin Ostkreuz", 52.5030, 13.4690, False),
            ("Cottbus", 51.7540, 14.3210, False),
            ("Angermünde", 53.0290, 13.9950, False),
            ("Bad Kleinen", 53.7580, 11.4870, False),
            # Krańce Polski — reguła nie może obcinać własnego kraju.
            ("Szczecin Główny", 53.4189, 14.5500, True),
            ("Hel", 54.6100, 18.8000, True),
            ("Terespol", 52.0740, 23.6120, True),
            ("Przemyśl", 49.7830, 22.7700, True),
            ("Zebrzydowice", 49.8760, 18.6110, True),
            ("Kołobrzeg", 54.1760, 15.5750, True),
        ],
    )
    def test_stacje_przygraniczne(self, polska, nazwa, lat, lon, w_polsce):
        assert polska.contains(lat, lon) is w_polsce, nazwa
