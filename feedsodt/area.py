"""Obszar, w którym umiemy policzyć geometrię przejazdu.

Silniki routujące znają tylko to, co jest w podanym wycinku OSM. Dla POLREGIO
jest to Polska, a feed sięga do Berlina, Cottbus i Bützow — i tam router nie
odmawia, tylko **przyczepia przystanek do najbliższego miejsca, które zna**.
Efekt jest gorszy niż brak kształtu: linia urywa się na granicy albo skacze
w losowe miejsce, a nic w danych nie mówi, że to nieprawda.

Stąd twarda reguła: kurs, który wyjeżdża poza obszar, nie dostaje kształtu
w ogóle. Nie zgadujemy po odległości dosunięcia ani po nazwie stacji, tylko
sprawdzamy położenie w granicy administracyjnej z OSM.

Granice leżą w ``feedsodt/data/<nazwa>-border.json``. Uzupełnienie o kolejny
kraj to dorzucenie pliku i wpisanie jego nazwy w ``shape_area``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


class AreaError(Exception):
    """Nie ma takiego obszaru albo plik z granicą jest nieczytelny."""


@dataclass(frozen=True)
class Area:
    """Wielokąt (albo ich zbiór) z testem przynależności punktu."""

    name: str
    #: Lista wielokątów; każdy to (pierścień zewnętrzny, dziury...).
    polygons: tuple[tuple[tuple[tuple[float, float], ...], ...], ...]
    bbox: tuple[float, float, float, float]  # min_lon, min_lat, max_lon, max_lat

    @classmethod
    def load(cls, name: str) -> Area:
        path = DATA / f"{name}-border.json"
        if not path.is_file():
            available = sorted(p.stem.removesuffix("-border") for p in DATA.glob("*-border.json"))
            raise AreaError(
                f"nieznany obszar {name!r}; dostępne: {', '.join(available) or 'brak'}"
            )
        try:
            geometry = json.loads(path.read_text(encoding="utf-8"))["geometry"]
        except (OSError, ValueError, KeyError) as exc:
            raise AreaError(f"{path}: nieczytelna granica ({exc})") from exc
        return cls.from_geometry(name, geometry)

    @classmethod
    def from_geometry(cls, name: str, geometry: dict) -> Area:
        kind = geometry.get("type")
        coordinates = geometry.get("coordinates") or []
        if kind == "Polygon":
            raw = [coordinates]
        elif kind == "MultiPolygon":
            raw = list(coordinates)
        else:
            raise AreaError(f"obszar {name!r}: oczekiwano Polygon/MultiPolygon, jest {kind!r}")

        polygons = tuple(
            tuple(tuple((float(x), float(y)) for x, y in ring) for ring in polygon)
            for polygon in raw
            if polygon and polygon[0]
        )
        if not polygons:
            raise AreaError(f"obszar {name!r}: pusta geometria")

        points = [point for polygon in polygons for ring in polygon for point in ring]
        bbox = (
            min(p[0] for p in points),
            min(p[1] for p in points),
            max(p[0] for p in points),
            max(p[1] for p in points),
        )
        return cls(name=name, polygons=polygons, bbox=bbox)

    def contains(self, lat: float, lon: float) -> bool:
        """Czy punkt leży wewnątrz obszaru.

        Najpierw bbox (odsiewa Berlin jednym porównaniem), potem promień
        poziomy dla każdego wielokąta, z odjęciem dziur.
        """
        min_lon, min_lat, max_lon, max_lat = self.bbox
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            return False
        for polygon in self.polygons:
            if _in_ring(lat, lon, polygon[0]) and not any(
                _in_ring(lat, lon, hole) for hole in polygon[1:]
            ):
                return True
        return False


def _in_ring(lat: float, lon: float, ring: tuple[tuple[float, float], ...]) -> bool:
    """Test promienia poziomego (ray casting) dla pierścienia [(lon, lat), ...]."""
    inside = False
    count = len(ring)
    previous = count - 1
    for current in range(count):
        x_current, y_current = ring[current]
        x_previous, y_previous = ring[previous]
        if (y_current > lat) != (y_previous > lat):
            crossing = (x_previous - x_current) * (lat - y_current) / (
                y_previous - y_current
            ) + x_current
            if lon < crossing:
                inside = not inside
        previous = current
    return inside
