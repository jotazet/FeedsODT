"""HTTP client for GraphHopper-compatible routing engines.

Skopiowane bez zmian z gtfs-shaper-maker (MIT, (c) 2025 jotazet):
https://github.com/jotazet/gtfs-shaper-maker/blob/main/router.py
rewizja 8a0ef3a36755a90f2ca303e69fa6ba761d7c5248 (2026-07-31).
Pełna treść licencji: shaper/LICENSE.gtfs-shaper-maker.

Trzymamy kopię zamiast zależności, bo gtfs-shaper-maker nie jest paczką
instalowalną (brak pyproject/setup.py), a to repozytorium nie jest gitem,
więc submoduł też odpada. Orkiestrację (grupowanie kursów, cache, zapis ZIP)
mamy własną — patrz feedsodt/shaping/__init__.py — ale samo dobijanie się do
silników routujących jest tutaj i ma pozostać zgodne z oryginałem.
"""

import math

import requests


class GraphHopperClient:
    def __init__(self, base_url, profile, gps_accuracy=50, timeout=60, fill_gaps=False):
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        self.gps_accuracy = gps_accuracy
        self.timeout = timeout
        # fill_gaps is OpenRailRouting-specific: route across gaps the matcher
        # could not snap, returning one continuous path.
        self.fill_gaps = fill_gaps
        self.session = requests.Session()

    def match(self, points):
        """Map-match a sequence of (lat, lon) points. Returns [[lon, lat], ...] or None."""
        params = {
            "profile": self.profile,
            "points_encoded": "false",
            "gps_accuracy": self.gps_accuracy,
        }
        if self.fill_gaps:
            params["fill_gaps"] = "true"
        try:
            response = self.session.post(
                f"{self.base_url}/match",
                params=params,
                data=_to_gpx(points).encode("utf-8"),
                headers={"Content-Type": "application/gpx+xml"},
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException:
            return None
        if response.status_code != 200:
            return None
        return _extract_coordinates(response.json())

    def route(self, points):
        """Route through a sequence of (lat, lon) points. Returns [[lon, lat], ...] or None."""
        body = {
            "profile": self.profile,
            # GraphHopper expects [lon, lat] pairs in JSON requests.
            "points": [[lon, lat] for lat, lon in points],
            "points_encoded": False,
            "instructions": False,
            "calc_points": True,
        }
        try:
            response = self.session.post(
                f"{self.base_url}/route", json=body, timeout=self.timeout
            )
        except requests.exceptions.RequestException:
            return None
        if response.status_code != 200:
            return None
        return _extract_coordinates(response.json())


# A match that leaves a straight-line jump this long between consecutive
# points has given up bridging the gap between two sparse stops instead of
# following the road - reject it and fall back to routing through the stops.
MAX_MATCH_GAP_M = 800


def fetch_trip_geometry(client, points):
    """Fetch the geometry for a trip's stop sequence of (lat, lon) points.

    Map matching only yields a clean shape when the engine can bridge the gaps
    between the sparse stop coordinates (OpenRailRouting's fill_gaps). Plain
    GraphHopper (buses) cannot, so matching sparse stops leaves straight-line
    gaps and skips stops on spurs/loops. For those engines we route THROUGH
    every stop instead, which visits each stop in order with no gaps.

    Returns (coords, method) where coords is [[lon, lat], ...] or None and
    method is one of 'match', 'route', 'route_endpoints', 'skipped'.
    """
    # Route through every stop first unless the engine can fill match gaps.
    if client.fill_gaps:
        order = ("match", "route")
    else:
        order = ("route", "match")

    for method in order:
        coords = client.match(points) if method == "match" else client.route(points)
        if not (coords and len(coords) >= 2):
            continue
        # For engines that cannot fill gaps (buses), a gappy match is worse
        # than no match - let the next method (routing) handle it. Engines that
        # do fill gaps (rail/tram) return continuous matches, so trust them.
        if method == "match" and not client.fill_gaps and _has_large_gap(coords, MAX_MATCH_GAP_M):
            continue
        return coords, method

    if len(points) >= 2:
        coords = client.route([points[0], points[-1]])
        if coords and len(coords) >= 2:
            return coords, "route_endpoints"

    return None, "skipped"


def _has_large_gap(coords, max_gap_m):
    """True if any two consecutive [lon, lat] points are farther than max_gap_m."""
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        if _haversine_m(lat1, lon1, lat2, lon2) > max_gap_m:
            return True
    return False


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _to_gpx(points):
    trkpts = "\n".join(
        f'      <trkpt lat="{lat}" lon="{lon}"/>' for lat, lon in points
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="gtfs-shaper-maker" xmlns="http://www.topografix.com/GPX/1/1">\n'
        "  <trk>\n"
        "    <trkseg>\n"
        f"{trkpts}\n"
        "    </trkseg>\n"
        "  </trk>\n"
        "</gpx>\n"
    )


def _extract_coordinates(data):
    paths = data.get("paths") or []
    if not paths:
        return None
    coords = (paths[0].get("points") or {}).get("coordinates") or []
    if not coords:
        return None
    # Drop the elevation component if present, keep [lon, lat].
    return [[point[0], point[1]] for point in coords]
