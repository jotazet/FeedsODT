"""Zbiorczy GTFS-RT VehiclePositions dla krajowego GTFS kolejowego (polish_trains.zip).

Dla każdego źródła z config/realtime.toml pobiera jego feed RT, dopasowuje pojazdy do kursów
krajowych i zapisuje jeden plik VehiclePositions z krajowymi trip_id + start_date. Pozycje
są prawdziwe (z feedów przewoźników); szacowane są tylko pozycje oczekiwane, którymi
rozstrzygamy dopasowanie.

Dopasowanie:
  A) po ID (źródło ma statyczny GTFS z tymi samymi trip_id co w RT):
       RT trip_id -> statyczny GTFS źródła (train_no_field) -> numer(y) pociągu
       (przewoźnik, numer, dzień) -> krajowy trip_id
     Gdy pasuje kilka kursów (LEG0/LEG1, wczoraj/dziś): stacja z RT na trasie,
     okno czasowe kursu, a na końcu najbliższa przewidywana pozycja.
  B) zapasowe (źródło bez statycznego GTFS z pasującymi ID):
       zgodność czasów z TripUpdate źródła z czasami krajowego RT + odległość od
       przewidywanej pozycji. Publikowane tylko przy jednoznacznym wyniku;
       raz ustalone dopasowanie jest pamiętane do końca dnia (state.json).
"""

from __future__ import annotations

import bisect
import csv
import datetime as dt
import io
import json
import logging
import math
import os
import threading
import time
import urllib.parse
import zipfile
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2 as rt

from ..http import USER_AGENT, build_session, download_to

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Warsaw")
STATION_RADIUS_KM = 1.5  # stacja z RT <-> stacja kursu krajowego
WINDOW_SLACK_S = 1800  # tolerancja okna czasowego kursu przy dopasowaniu po ID
SIG_TOL_S = 90  # tolerancja porównania czasów TU (dopasowanie zapasowe)
#: Feedy RT odświeżają się co kilkanaście sekund: bez ponawiania, następny przebieg i tak
#: przyjdzie za chwilę, a ponowienia z backoffem przeciągnęłyby jeden przebieg w nieskończoność.
RT_TIMEOUT = (10, 30)


# ───────────────────────── narzędzia ─────────────────────────
def rt_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def http_get(session: requests.Session, url: str) -> bytes:
    """GET; login i hasło w adresie (user:pass@) requests zamienia na Basic Auth."""
    response = session.get(url, timeout=RT_TIMEOUT)
    response.raise_for_status()
    return response.content


def cached(url: str, path: Path, max_age_h: float) -> Path:
    """Plik z `url`, pobrany ponownie, gdy kopia w `path` jest starsza niż max_age_h."""
    if path.exists() and time.time() - path.stat().st_mtime < max_age_h * 3600:
        return path
    log.info("pobieram %s -> %s", urllib.parse.urlsplit(url).hostname, path.name)
    # Wątki TU i VP potrafią pobierać tę samą kopię naraz.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with build_session() as session:
        download_to(session, url, tmp, path.name)
    os.replace(tmp, path)
    return path


def zip_csv(zf: zipfile.ZipFile, name: str):
    """Czyta plik CSV z archiwum (także gdy leży w podkatalogu)."""
    cand = [n for n in zf.namelist() if n == name or n.endswith("/" + name)]
    if not cand:
        return iter(())
    return csv.DictReader(io.TextIOWrapper(zf.open(cand[0]), encoding="utf-8-sig"))


def load_feed(data: bytes) -> rt.FeedMessage:
    m = rt.FeedMessage()
    m.ParseFromString(data)
    return m


def write_feed(feed: rt.FeedMessage, path: Path) -> None:
    """Zapis atomowy: odbiorca nigdy nie pobierze połowy pliku."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(feed.SerializeToString())
    os.replace(tmp, path)


def train_numbers(s):
    """'11276/11277' -> {11276,11277}; '69304/5 Kwisa' -> {69304,69305}; '66816/66817/5316' -> 3 liczby."""
    out = set()
    tok = (s or "").strip().split(" ")[0]
    parts = [p for p in tok.split("/") if p.isdigit()]
    if not parts:
        return out
    base = parts[0]
    out.add(int(base))
    for p in parts[1:]:
        if len(p) < len(base):
            p = base[: len(base) - len(p)] + p
        out.add(int(p))
    return out


def km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 12742 * math.asin(math.sqrt(a))


def gsecs(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def day_base(datestr):
    """Początek dnia usługi GTFS: południe minus 12 h, poprawnie przy zmianie czasu."""
    d = dt.date(int(datestr[:4]), int(datestr[4:6]), int(datestr[6:]))
    return int(dt.datetime(d.year, d.month, d.day, 12, tzinfo=TZ).timestamp()) - 12 * 3600


# ───────────────────────── krajowy GTFS ─────────────────────────
class National:
    def __init__(self, zip_path, agencies, dates):
        self.dates = dates
        with zipfile.ZipFile(zip_path) as zf:
            self.stops = {}
            for r in zip_csv(zf, "stops.txt"):
                self.stops[r["stop_id"]] = (
                    float(r["stop_lat"]),
                    float(r["stop_lon"]),
                    r.get("parent_station") or r["stop_id"],
                    r.get("stop_name", ""),
                )
            route_ag = {r["route_id"]: r["agency_id"] for r in zip_csv(zf, "routes.txt")}
            svc = defaultdict(set)
            for r in zip_csv(zf, "calendar.txt"):
                for d in dates:
                    wd = dt.date(int(d[:4]), int(d[4:6]), int(d[6:])).strftime("%A").lower()
                    if r["start_date"] <= d <= r["end_date"] and r.get(wd) == "1":
                        svc[r["service_id"]].add(d)
            for r in zip_csv(zf, "calendar_dates.txt"):
                if r["date"] in dates:
                    if r["exception_type"] == "1":
                        svc[r["service_id"]].add(r["date"])
                    else:
                        svc[r["service_id"]].discard(r["date"])
            self.trips = {}
            self.by_num = defaultdict(list)  # (agency, nr) -> [(trip_id, date)]
            self.by_agency = defaultdict(list)  # agency -> [(trip_id, date)]
            for r in zip_csv(zf, "trips.txt"):
                ag = route_ag.get(r["route_id"])
                if ag not in agencies or not svc.get(r["service_id"]):
                    continue
                self.trips[r["trip_id"]] = {"route_id": r["route_id"], "agency": ag}
                nums = train_numbers(r.get("trip_short_name")) | train_numbers(
                    r.get("plk_train_number")
                )
                for d in svc[r["service_id"]]:
                    self.by_agency[ag].append((r["trip_id"], d))
                    for n in nums:
                        self.by_num[(ag, n)].append((r["trip_id"], d))
            st = defaultdict(list)
            for r in zip_csv(zf, "stop_times.txt"):
                if r["trip_id"] in self.trips:
                    st[r["trip_id"]].append(
                        (
                            int(r["stop_sequence"]),
                            r["stop_id"],
                            gsecs(r["arrival_time"]),
                            gsecs(r["departure_time"]),
                        )
                    )
            self.st = {k: sorted(v) for k, v in st.items()}
        self.pred, self.canceled = {}, set()
        log.info(
            "krajowy GTFS: %d kursów (%s) w dniach %s",
            len(self.trips),
            ", ".join(sorted(agencies)),
            dates,
        )

    def set_rt(self, feed):
        self.pred, self.canceled = {}, set()
        for e in feed.entity:
            if not e.HasField("trip_update"):
                continue
            tu = e.trip_update
            key = (tu.trip.trip_id, tu.trip.start_date)
            if tu.trip.schedule_relationship == rt.TripDescriptor.CANCELED:
                self.canceled.add(key)
            p = {}
            for s in tu.stop_time_update:
                a = s.arrival.time if s.HasField("arrival") else 0
                b = s.departure.time if s.HasField("departure") else 0
                if a or b:
                    p[s.stop_sequence] = (a or b, b or a)
            self.pred[key] = p

    def timeline(self, tid, d):
        """[(arr, dep, stop_id, seq)] w czasie absolutnym, z krajowym RT i propagacją opóźnienia."""
        base, p, delay, out = day_base(d), self.pred.get((tid, d), {}), 0, []
        for seq, sid, a, b in self.st.get(tid, []):
            if seq in p:
                out.append((p[seq][0], p[seq][1], sid, seq))
                delay = p[seq][1] - (base + b)
            else:
                out.append((base + a + delay, base + b + delay, sid, seq))
        return out

    def position_at(self, tl, t):
        if not tl:
            return None
        if t <= tl[0][1]:
            return self.stops[tl[0][2]][:2]
        for (_a0, d0, s0, _), (a1, _d1, s1, _) in zip(tl, tl[1:]):
            if t <= d0:
                return self.stops[s0][:2]
            if t < a1:
                f = (t - d0) / max(1, a1 - d0)
                p0, p1 = self.stops[s0], self.stops[s1]
                return p0[0] + f * (p1[0] - p0[0]), p0[1] + f * (p1[1] - p0[1])
        return self.stops[tl[-1][2]][:2]

    def stop_near(self, tid, lat, lon):
        """Najbliższy postój kursu krajowego do punktu (seq, stop_id, km)."""
        best = None
        for seq, sid, _, _ in self.st.get(tid, []):
            s = self.stops[sid]
            d = km(lat, lon, s[0], s[1])
            if best is None or d < best[2]:
                best = (seq, sid, d)
        return best


# ───────────────────────── statyczny GTFS źródła ─────────────────────────
class SourceStatic:
    def __init__(self, zip_path, field):
        with zipfile.ZipFile(zip_path) as zf:
            self.nums = {r["trip_id"]: train_numbers(r.get(field)) for r in zip_csv(zf, "trips.txt")}
            self.stops = {
                r["stop_id"]: (float(r["stop_lat"]), float(r["stop_lon"]))
                for r in zip_csv(zf, "stops.txt")
                if r.get("stop_lat")
            }


# ───────────────────────── dopasowanie ─────────────────────────
def tu_times(tu):
    return [x for s in tu.stop_time_update for x in (s.arrival.time, s.departure.time) if x]


def signature(times, tl):
    """Jaki ułamek czasów z TU źródła ma odpowiednik w osi czasu kursu krajowego."""
    if not times or not tl:
        return None
    cand = sorted(x for a, b, _, _ in tl for x in (a, b))
    hit = 0
    for t in times:
        i = bisect.bisect_left(cand, t)
        if min((abs(cand[j] - t) for j in (i - 1, i) if 0 <= j < len(cand)), default=1e9) <= SIG_TOL_S:
            hit += 1
    return hit / len(times)


def match_by_id(nat, src, agency, v, t, stop_ll):
    nums = src.nums.get(v.trip.trip_id)
    if nums is None:
        return None, "trip_id nieznany w statycznym GTFS źródła"
    cands = {c for n in nums for c in nat.by_num.get((agency, n), [])}
    if not cands:  # np. pociąg prowadzony taborem przewoźnika, ale zapisany pod innym (IC/PR)
        cands = {c for (_ag, n), lst in nat.by_num.items() if n in nums for c in lst}
    cands = {c for c in cands if c not in nat.canceled}
    if not cands:
        return None, "numer pociągu nie występuje w krajowym GTFS dziś/wczoraj"
    how = ["id"]
    if len(cands) > 1 and stop_ll:
        f = {
            c
            for c in cands
            if (s := nat.stop_near(c[0], *stop_ll)) and s[2] <= STATION_RADIUS_KM
        }
        if f:
            cands = f
            how.append("stacja")
    tls = {c: nat.timeline(*c) for c in cands}
    # Okno czasowe jest obowiązkowe: źródło potrafi trzymać trip_id długo po dojechaniu
    # do stacji końcowej.
    cands = {
        c
        for c in cands
        if tls[c] and tls[c][0][1] - WINDOW_SLACK_S <= t <= tls[c][-1][0] + WINDOW_SLACK_S
    }
    if not cands:
        return None, "kurs poza oknem czasowym (pojazd stoi po zakończeniu kursu?)"
    lat, lon = v.position.latitude, v.position.longitude
    best = min(cands, key=lambda c: km(lat, lon, *(nat.position_at(tls[c], t) or (0, 0))))
    if len(cands) > 1:
        how.append("pozycja")
    dist = km(lat, lon, *(nat.position_at(tls[best], t) or (0, 0)))
    return (best, "+".join(how), dist), None


def match_fallback(nat, agency, v, t, times):
    lat, lon = v.position.latitude, v.position.longitude
    scored = []
    for c in nat.by_agency.get(agency, []):
        if c in nat.canceled:
            continue
        tl = nat.timeline(*c)
        if not tl or not (tl[0][1] - 900 <= t <= tl[-1][0] + 900):
            continue
        p = nat.position_at(tl, t)
        d = km(lat, lon, *p)
        if d > 5:
            continue
        scored.append((signature(times, tl), d, c))
    if not scored:
        return None, "brak kursu krajowego w pobliżu"
    if times:
        scored.sort(key=lambda x: (-(x[0] or 0), x[1]))
        b = scored[0]
        if (b[0] or 0) < 0.75:
            return None, f"zbyt słaba zgodność czasów ({b[0]:.0%})"
        if len(scored) > 1:
            s2 = scored[1]
            if (b[0] - (s2[0] or 0)) < 0.2 and (s2[1] - b[1]) < 1.5:
                return None, "niejednoznaczne (kilka kursów pasuje)"
        return (b[2], f"fallback:czasy {b[0]:.0%}", b[1]), None
    scored.sort(key=lambda x: x[1])
    b = scored[0]
    if b[1] > 1.0 or (len(scored) > 1 and scored[1][1] - b[1] < 2.0):
        return None, "niejednoznaczne (brak TU, tylko pozycja)"
    return (b[2], "fallback:pozycja", b[1]), None


# ───────────────────────── przebieg ─────────────────────────
class VPBuilder:
    def __init__(self, cfg: dict, cache_dir: Path):
        self.cfg, self.cache = cfg, cache_dir
        self.state_path = cache_dir / "state.json"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.nat, self.nat_key, self.src = None, None, {}
        self.session = rt_session()
        try:
            self.state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            self.state = {}

    def ensure_static(self, now):
        age = self.cfg["static_max_age_hours"]
        today = dt.datetime.fromtimestamp(now, TZ).date()
        dates = [(today - dt.timedelta(days=1)).strftime("%Y%m%d"), today.strftime("%Y%m%d")]
        zp = cached(self.cfg["national"]["static"], self.cache / "national.zip", age)
        key = (tuple(dates), zp.stat().st_mtime)
        if key != self.nat_key:
            self.nat = National(zp, {s["agency"] for s in self.cfg["sources"]}, dates)
            self.nat_key = key
        for s in self.cfg["sources"]:
            if s.get("static"):
                p = cached(s["static"], self.cache / f"{s['name']}.zip", age)
                if self.src.get(s["name"], (None, 0))[1] != p.stat().st_mtime:
                    field = s.get("train_no_field", "trip_short_name")
                    self.src[s["name"]] = (SourceStatic(p, field), p.stat().st_mtime)
        self.state = {d: v for d, v in self.state.items() if d in dates}
        return dates[-1]

    def run_once(self, out_path: Path, rt_feed=None):
        """rt_feed: własny TripUpdates (plk.py); bez niego zapasowy national.rt z konfiguracji."""
        now = int(time.time())
        today = self.ensure_static(now)
        if rt_feed is None and self.cfg["national"].get("rt"):
            rt_feed = load_feed(http_get(self.session, self.cfg["national"]["rt"]))
        self.nat.set_rt(rt_feed if rt_feed is not None else rt.FeedMessage())
        out = rt.FeedMessage()
        out.header.gtfs_realtime_version = "2.0"
        out.header.incrementality = rt.FeedHeader.FULL_DATASET
        summary = {}
        mem = self.state.setdefault(today, {})
        for s in self.cfg["sources"]:
            name, ag = s["name"], s["agency"]
            stats = defaultdict(int)
            feeds = []
            for u in s["rt"]:
                try:
                    feeds.append(load_feed(http_get(self.session, u)))
                except Exception as ex:  # jedno źródło nie może zatrzymać całości
                    stats[f"błąd pobierania: {ex.__class__.__name__}"] += 1
            tu_by_trip = {}
            vps = {}
            for f in feeds:
                for e in f.entity:
                    if e.HasField("trip_update"):
                        tu_by_trip[e.trip_update.trip.trip_id] = tu_times(e.trip_update)
                    if e.HasField("vehicle") and e.vehicle.HasField("position"):
                        vps[e.vehicle.vehicle.id or e.id] = e.vehicle
            static = self.src.get(name, (None,))[0]
            smem = mem.setdefault(name, {})
            for vid, v in vps.items():
                t = v.timestamp or now
                if not v.trip.trip_id:
                    stats["pojazd bez kursu w źródle (pominięty)"] += 1
                    continue
                stop_ll = static.stops.get(v.stop_id) if (static and v.stop_id) else None
                if static:
                    res, err = match_by_id(self.nat, static, ag, v, t, stop_ll)
                elif v.trip.trip_id in smem and tuple(smem[v.trip.trip_id]) in set(
                    self.nat.by_agency.get(ag, [])
                ):
                    c = tuple(smem[v.trip.trip_id])
                    tl = self.nat.timeline(*c)
                    if tl and tl[0][1] - WINDOW_SLACK_S <= t <= tl[-1][0] + WINDOW_SLACK_S:
                        p = self.nat.position_at(tl, t)
                        dist = km(v.position.latitude, v.position.longitude, *p)
                        res, err = (c, "fallback:zapamiętane", dist), None
                    else:
                        res, err = None, "kurs poza oknem czasowym (pojazd stoi po zakończeniu kursu?)"
                else:
                    res, err = match_fallback(self.nat, ag, v, t, tu_by_trip.get(v.trip.trip_id, []))
                    if res:
                        smem[v.trip.trip_id] = list(res[0])
                if not res:
                    stats[err] += 1
                    continue
                (tid, d), _how, dist = res
                stats["dopasowane"] += 1
                if dist > 10:
                    stats["ostrzeżenie: >10 km od przewidywanej pozycji"] += 1
                oe = out.entity.add()
                oe.id = f"{name}:{vid}"
                ov = oe.vehicle
                ov.trip.trip_id, ov.trip.start_date = tid, d
                ov.trip.route_id = self.nat.trips[tid]["route_id"]
                ov.trip.schedule_relationship = rt.TripDescriptor.SCHEDULED
                ov.vehicle.id = f"{ag}:{v.vehicle.id or vid}"
                ov.vehicle.label = v.vehicle.label or v.vehicle.id or vid
                if v.vehicle.license_plate:
                    ov.vehicle.license_plate = v.vehicle.license_plate
                ov.position.CopyFrom(v.position)
                ov.timestamp = t
                if (
                    stop_ll
                    and (sn := self.nat.stop_near(tid, *stop_ll))
                    and sn[2] <= STATION_RADIUS_KM
                ):
                    ov.current_stop_sequence, ov.stop_id = sn[0], sn[1]
                    if v.HasField("current_status"):
                        ov.current_status = v.current_status
            summary[name] = dict(stats)
        out.header.timestamp = now
        write_feed(out, out_path)
        self.state_path.write_text(json.dumps(self.state))
        log.info("VP: %d pojazdów -> %s", len(out.entity), out_path.name)
        for k, v in summary.items():
            log.info("  %-4s %s", k, v)
        return out, summary
