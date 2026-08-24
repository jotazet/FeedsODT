"""Budowanie sztucznych paczek GTFS na potrzeby testów.

Testy nie dotykają sieci. Paczki powstają w `tmp_path`, więc każdy przypadek ma
własne, czytelne dane — dużo łatwiej zrozumieć, co się testuje, niż przy
podpinaniu prawdziwych 59 MB z GZM.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def feed_tables(
    *,
    start: date,
    end: date,
    service_id: str = "S1",
    trip_ids: tuple[str, ...] = ("T1",),
    route_id: str = "R1",
    route_long_name: str = "RYNEK - DWORZEC",
    stop_ids: tuple[str, ...] = ("ST1", "ST2"),
    shape_id: str = "SH1",
    with_feed_info: bool = True,
) -> dict[str, list[dict[str, str]]]:
    """Komplet tabel minimalnego, poprawnego feedu. Każdy parametr da się nadpisać,
    żeby test mówił wprost, co jest w danym przypadku istotne."""
    tables: dict[str, list[dict[str, str]]] = {
        "agency.txt": [
            {
                "agency_id": "A1",
                "agency_name": "Testowy Przewoźnik",
                "agency_url": "https://example.pl",
                "agency_timezone": "Europe/Warsaw",
            }
        ],
        "stops.txt": [
            {"stop_id": s, "stop_name": f"Przystanek {s}", "stop_lat": "50.06", "stop_lon": "19.94"}
            for s in stop_ids
        ],
        "routes.txt": [
            {
                "route_id": route_id,
                "agency_id": "A1",
                "route_short_name": "1",
                "route_long_name": route_long_name,
                "route_type": "3",
            }
        ],
        "trips.txt": [
            {"route_id": route_id, "service_id": service_id, "trip_id": t, "shape_id": shape_id}
            for t in trip_ids
        ],
        "stop_times.txt": [
            {
                "trip_id": t,
                "arrival_time": f"{8 + index:02d}:00:00",
                "departure_time": f"{8 + index:02d}:00:00",
                "stop_id": stop,
                "stop_sequence": str(index + 1),
            }
            for t in trip_ids
            for index, stop in enumerate(stop_ids)
        ],
        "calendar.txt": [
            {
                "service_id": service_id,
                **{day: "1" for day in WEEKDAYS},
                "start_date": f"{start:%Y%m%d}",
                "end_date": f"{end:%Y%m%d}",
            }
        ],
        "shapes.txt": [
            {
                "shape_id": shape_id,
                "shape_pt_lat": "50.06",
                "shape_pt_lon": f"19.9{point}",
                "shape_pt_sequence": str(point + 1),
            }
            for point in range(3)
        ],
    }
    if with_feed_info:
        tables["feed_info.txt"] = [
            {
                "feed_publisher_name": "Test",
                "feed_publisher_url": "https://example.pl",
                "feed_lang": "pl",
                "feed_start_date": f"{start:%Y%m%d}",
                "feed_end_date": f"{end:%Y%m%d}",
            }
        ]
    return tables


def write_gtfs(path: Path, tables: dict[str, list[dict[str, str]]]) -> Path:
    """Zapisuje tabele jako archiwum ZIP. Pusta lista = plik z samym nagłówkiem
    nie powstaje wcale (tak jak u części przewoźników)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, rows in tables.items():
            if not rows:
                continue
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(name, buffer.getvalue())
    return path


def read_gtfs(path: Path, name: str) -> list[dict[str, str]]:
    """Odczyt tabeli ze scalonego pliku — do asercji w testach."""
    with zipfile.ZipFile(path) as archive:
        if name not in archive.namelist():
            return []
        raw = archive.read(name).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(raw)))


@pytest.fixture
def build(tmp_path: Path):
    """Fabryka paczek: build("a.zip", start=..., end=..., trip_ids=("T1",))."""

    def make(filename: str, **kwargs) -> Path:
        return write_gtfs(tmp_path / filename, feed_tables(**kwargs))

    return make
