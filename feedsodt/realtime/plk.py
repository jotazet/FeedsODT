"""GTFS-RT TripUpdates z otwartego API PKP PLK (pdp-api.plk-sa.pl) dla krajowego GTFS
kolejowego https://mkuran.pl/gtfs/polish_trains.zip.

Logika przeniesiona z MKuranowski/PolishTrainsGTFS (polish_trains_gtfs/realtime),
Copyright (c) 2025-2026 Mikołaj Kuranowski, licencja MIT (LICENSE.PolishTrainsGTFS obok):
  * GET /api/v1/operations/shortened?fullRoutes=true&carriersExclude=WKD  (strony po 10 000)
  * dopasowanie pociągu: (scheduleId, orderId, operatingDate) <-> trip_id PLK_<przewoźnik>_<sid>_<oid>[_LEGn]
    + service_id z przesunięciem dnia (np. PLK_12+1D) -> start_date GTFS
  * zapasowo: /api/v1/schedules/shortened -> (przewoźnik, numer, dzień PLK) -> kurs po numerze
  * stop_time_update po plk_sequence; odwołane stacje -> SKIPPED, cały kurs -> CANCELED,
    stacja spoza rozkładu -> objazd (REPLACEMENT, przystanki <id>_FALLBACK)
  * uncertainty 0 = czas potwierdzony (cf), 1 = prognoza
  * odstępstwo od oryginału: czasy w kursie nie mogą się cofać (monotonic)

Dane: PKP Polskie Linie Kolejowe S.A., Otwarte Dane Kolejowe (regulamin API wymaga
wskazania PLK jako źródła).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2 as rt

from ..http import USER_AGENT
from .vehicles import TZ, cached, write_feed, zip_csv

log = logging.getLogger(__name__)

API = "https://pdp-api.plk-sa.pl/api/v1"
PAGE_SIZE, MAX_PAGES, PAGE_SPACING_S = 10_000, 5, 1.0
ALT_LOOKUP_PERIOD_S = 3600  # Kuranowski: 20 min; godzina wystarcza (rozkład zmienia się rzadko)
API_TIMEOUT = (10, 120)

OFFSET_RE = re.compile(r"([+-][0-9]+)D$")
TRIP_RE = re.compile(r"^PLK_([A-Za-z0-9]+)_([0-9]+)_([0-9]+)")
NUMBER_RE = re.compile(r"^[0-9]{3,6}(?:/[0-9])?")
CARRIER_TO_AGENCY = {"KMŁ": "KML", "Leo Express": "LEO", "ŁKA": "LKA", "PAR-WOL": "PW"}


class RateLimited(Exception):
    pass


# ───────────────────────── narzędzia ─────────────────────────
def ymd(s):
    """'2026-11-15', '20261115', '2026-11-15T00:00:00' -> date"""
    s = (s or "").replace("-", "")[:8]
    return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def parse_local(s):
    """Czas z API: RFC3339 z przesunięciem albo czas lokalny (Europe/Warsaw) -> unix."""
    if not s:
        return 0
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=TZ)
    return int(t.timestamp())


def expand_number(s):
    parts = s.split("/") if s else []
    if len(parts) == 2 and len(parts[1]) == 1:  # '69304/5' -> 69304, 69305
        parts[1] = parts[0][:-1] + parts[1]
    return [p for p in parts if p]


def numbers_of(row):
    m = NUMBER_RE.match(row.get("trip_short_name") or "")
    nums = expand_number(m.group(0)) if m else []
    for n in expand_number(row.get("plk_train_number") or ""):
        if n not in nums:
            nums.append(n)
    return nums


# ───────────────────────── statyczny GTFS (mkuran) ─────────────────────────
class PlkStatic:
    """Indeks kursów PLK_* z polish_trains.zip na kilka dni wokół dziś."""

    def __init__(self, zip_path, plk_dates):
        plk_dates = set(plk_dates)
        with zipfile.ZipFile(zip_path) as zf:
            fi = next(zip_csv(zf, "feed_info.txt"))
            self.feed_start = ymd(fi["feed_start_date"]) - dt.timedelta(days=1)
            self.feed_end = ymd(fi["feed_end_date"])
            self.version = fi.get("feed_version", "")

            # stops: id stacji z API (liczba) -> kanoniczny stop_id
            all_ids, secondary = defaultdict(list), {}
            for r in zip_csv(zf, "stops.txt"):
                station = r["stop_id"].split("_", 1)[0]
                all_ids[station].append(r["stop_id"])
                if r.get("plk_secondary_id"):
                    secondary[r["plk_secondary_id"]] = station

            def rank(i):
                return 3 if i.endswith("_RAIL") else 2 if i.endswith("_BUS") else 1 if "_BUS_" in i else 0

            self.canon = {}
            for station, ids in all_ids.items():
                best = max(ids, key=rank)  # max() zwraca pierwszy maksymalny, jak slices.MaxFunc
                self.canon[station] = best
                for i in ids:
                    self.canon[i] = best
            for sec, station in secondary.items():
                self.canon[sec] = self.canon[station]
            self.canon["34868"] = "33506"  # W-wa Zachodnia peron 9 raportowana jako W-wa Zachodnia

            # calendar_dates: service_id -> [(data GTFS, data PLK)]
            services = defaultdict(list)
            for r in zip_csv(zf, "calendar_dates.txt"):
                if r["exception_type"] != "1":
                    continue
                g = ymd(r["date"])
                m = OFFSET_RE.search(r["service_id"])
                p = g - dt.timedelta(days=int(m.group(1)) if m else 0)
                if p in plk_dates:
                    services[r["service_id"]].append((g, p))

            # trips: klucz (sid, oid, data PLK) -> kurs
            self.trips = {}
            tid_keys = defaultdict(list)
            for r in zip_csv(zf, "trips.txt"):
                tid = r["trip_id"]
                if not tid.startswith("PLK_") or not services.get(r["service_id"]):
                    continue
                m = TRIP_RE.match(tid)
                if not m:
                    continue
                ag, sid, oid = m.group(1), int(m.group(2)), int(m.group(3))
                nums = numbers_of(r)
                for g, p in services[r["service_id"]]:
                    key = (sid, oid, p)
                    t = self.trips.get(key)
                    if t is None or t["gtfs_date"] != g:
                        t = self.trips[key] = {
                            "agency": ag,
                            "gtfs_date": g,
                            "plk_date": p,
                            "numbers": nums,
                            "trip_ids": [],
                            "st": [],
                        }
                    if tid not in t["trip_ids"]:
                        t["trip_ids"].append(tid)
                    tid_keys[tid].append(key)

            # stop_times (tylko kursy PLK z wybranych dni)
            st_by_tid = defaultdict(list)
            for r in zip_csv(zf, "stop_times.txt"):
                tid = r["trip_id"]
                if tid not in tid_keys:
                    continue
                station = r["stop_id"].split("_", 1)[0]
                st_by_tid[tid].append(
                    (
                        int(r["stop_sequence"]),
                        int(r["plk_sequence"]),
                        tid,
                        self.canon.get(station, station),
                        r.get("platform", ""),
                        r.get("track", ""),
                    )
                )
            for tid, keys in tid_keys.items():
                for k in set(keys):
                    self.trips[k]["st"].extend(st_by_tid.get(tid, []))
            for t in self.trips.values():
                t["st"].sort()
                t["stop_ids"] = {s[3] for s in t["st"]}

        # agencja + numer + data PLK -> kurs (niejednoznaczne = None)
        self.by_number = {}
        for t in self.trips.values():
            for n in t["numbers"]:
                k = (t["agency"], n, t["plk_date"])
                self.by_number[k] = None if k in self.by_number else t
        self.alt = {}  # (sid, oid, data PLK) -> (agencja, numer, data PLK) z /schedules/shortened
        log.info(
            "polish_trains.zip %s: %d kursów PLK w dniach PLK %s",
            self.version,
            len(self.trips),
            ", ".join(d.isoformat() for d in sorted(plk_dates)),
        )

    def contains(self, d):
        return self.feed_start <= d <= self.feed_end

    def trip(self, sid, oid, od):
        t = self.trips.get((sid, oid, od))
        if t is not None:
            return t, "id"
        nk = self.alt.get((sid, oid, od))
        if nk:
            t = self.by_number.get(nk)
            if t is not None:
                return t, "numer"
        return None, None


# ───────────────────────── klient API ─────────────────────────
class PlkApi:
    """Bez automatycznych ponowień: każde zapytanie liczy się do limitu planu."""

    def __init__(self, apikey, min_spacing=PAGE_SPACING_S):
        self.spacing, self.last = min_spacing, 0.0
        self.limits = {}
        self.requests = 0
        self.session = requests.Session()
        self.session.headers.update(
            {"X-Api-Key": apikey, "Accept": "application/json", "User-Agent": USER_AGENT}
        )

    def get(self, path, params):
        wait = self.last + self.spacing - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        self.requests += 1
        response = self.session.get(f"{API}{path}", params=params, timeout=API_TIMEOUT)
        self._limits(response.headers)
        if response.status_code == 429:
            raise RateLimited(f"429 limit zapytań: {response.text[:300]}")
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code} {path}: {response.text[:300]}")
        return response.json()

    def _limits(self, h):
        for k in ("Hourly-Limit", "Hourly-Remaining", "Daily-Limit", "Daily-Remaining"):
            v = h.get(f"X-RateLimit-{k}")
            if v is not None:
                self.limits[k] = int(v) if v.lstrip("-").isdigit() else v

    def operations(self):
        trains, ts, buster = [], None, int(time.time())
        for page in range(1, MAX_PAGES + 1):
            o = self.get(
                "/operations/shortened",
                {
                    "page": page,
                    "pageSize": PAGE_SIZE,
                    "fullRoutes": "true",
                    # znacznik czasu omija cache CDN (tak robi oryginał)
                    "carriersExclude": f"WKD,{buster}",
                },
            )
            ts = ts or o.get("ts")
            trains.extend(o.get("tr") or [])
            if not (o.get("pg") or {}).get("hn"):
                return ts, trains
        raise RuntimeError(f"operacje zajmują więcej niż {MAX_PAGES} stron")

    def schedules_shortened(self, d_from, d_to):
        return self.get(
            "/schedules/shortened",
            {"dateFrom": d_from.isoformat(), "dateTo": d_to.isoformat(), "carriersExclude": "WKD"},
        )


# ───────────────────────── TripUpdates ─────────────────────────
def monotonic(stop_times):
    """Usuwa cofanie się czasu w kursie (przyjazd po odjeździe, stacja przed poprzednią).

    API PLK zostawia czasem stary czas z rozkładu obok opóźnionego; walidator GTFS-RT
    uznaje to za błąd (E022, E025), a OpenTripPlanner odrzuca wtedy całą aktualizację
    kursu. Nie wiadomo, który z dwóch sprzecznych czasów jest zły, więc zostaje
    najdłuższy niemalejący ciąg czasów kursu, a każdy czas spoza niego dostaje najbliższy
    sąsiedni z tego ciągu i jest oznaczany jako prognoza. Jeden zawyżony czas nie ciągnie
    wtedy w górę reszty kursu. Zwraca liczbę poprawionych czasów."""
    events = [
        (s, ev)
        for s in stop_times
        if not s.get("cancelled")
        for ev in ("arrival", "departure")
        if s.get(ev)
    ]
    times = [s[ev] for s, ev in events]
    if all(a <= b for a, b in zip(times, times[1:])):
        return 0
    keep = _longest_non_decreasing(times)
    following, upcoming = [None] * len(times), None
    for i in reversed(range(len(times))):
        if i in keep:
            upcoming = times[i]
        following[i] = upcoming
    fixed, previous = 0, None
    for i, (s, ev) in enumerate(events):
        if i in keep:
            previous = times[i]
            continue
        # Spoza ciągu: albo niższy od poprzedniego zachowanego, albo wyższy od następnego.
        s[ev] = previous if previous is not None and times[i] < previous else following[i]
        s["confirmed"] = False
        fixed += 1
    return fixed


def _longest_non_decreasing(xs):
    """Indeksy najdłuższego niemalejącego podciągu. O(n²), ale woła się tylko dla kursów
    z cofającym się czasem (ok. 100 na kilkanaście tysięcy)."""
    length, prev = [1] * len(xs), [-1] * len(xs)
    for i in range(len(xs)):
        for j in range(i):
            if xs[j] <= xs[i] and length[j] + 1 > length[i]:
                length[i], prev[i] = length[j] + 1, j
    i, out = max(range(len(xs)), key=length.__getitem__), set()
    while i >= 0:
        out.add(i)
        i = prev[i]
    return out


def build_updates(static, trains, stats):
    """Lista słowników TU (format jak live.json Kuranowskiego)."""
    out = []
    for tr in trains:
        try:
            od = ymd(tr["od"])
        except (KeyError, ValueError):
            stats["błędny rekord"] += 1
            continue
        trip, how = static.trip(tr.get("sid"), tr.get("oid"), od)
        if trip is None:
            stats["poza zakresem dat GTFS" if not static.contains(od) else "niedopasowane"] += 1
            continue
        stats[f"dopasowane ({how})"] += 1
        stops = sorted(tr.get("st") or [], key=lambda s: s.get("asn") or 0)

        def base(tid, trip=trip):
            return {
                "id": f"U_{trip['gtfs_date']:%Y%m%d}_{tid}",
                "trip_id": tid,
                "start_date": f"{trip['gtfs_date']:%Y%m%d}",
                "agency_id": trip["agency"],
                "numbers": trip["numbers"],
                "stop_times": [],
            }

        tids = trip["trip_ids"]

        cancelled = (tr.get("s") == "X") if not stops else all(s.get("cn") for s in stops)
        if cancelled:
            out += [dict(base(t), cancelled=True) for t in tids]
            stats["kurs odwołany"] += 1
            continue

        sched = set(trip["stop_ids"])
        if "179223" in sched and "75507" in sched:  # Bohumin–Zebrzydowice: Petrovice u Karviné
            sched.add("179221")
        real_ids = [static.canon.get(str(s.get("id")), "") for s in stops]
        if any(i and i not in sched for i in real_ids):
            first = base(tids[0])
            first["detour"] = True
            seq = 1000
            for s, sid in zip(stops, real_ids):
                if sid and not s.get("cn"):
                    first["stop_times"].append(
                        {
                            "stop_sequence": seq,
                            "stop_id": sid + "_FALLBACK",
                            "arrival": parse_local(s.get("aa")),
                            "departure": parse_local(s.get("ad")),
                            "confirmed": bool(s.get("cf")),
                        }
                    )
                    seq += 1
            stats["czasy cofnięte -> poprawione"] += monotonic(first["stop_times"])
            out.append(first)
            out += [dict(base(t), cancelled=True) for t in tids[1:]]
            stats["objazd"] += 1
            continue

        by_psn = {s.get("psn"): s for s in stops}
        ups = {t: base(t) for t in tids}
        for gseq, pseq, tid, _sid, platform, track in trip["st"]:
            s = by_psn.get(pseq)
            if s is None:
                continue
            u = {"stop_sequence": gseq}
            if s.get("cn"):
                u["cancelled"] = True
            else:
                u.update(
                    arrival=parse_local(s.get("aa")),
                    departure=parse_local(s.get("ad")),
                    confirmed=bool(s.get("cf")),
                    platform=platform,
                    track=track,
                )
            ups[tid]["stop_times"].append(u)
        for u in ups.values():
            stats["czasy cofnięte -> poprawione"] += monotonic(u["stop_times"])
        out += [u for u in ups.values() if u["stop_times"]]
    return out


def to_feed(updates, ts):
    m = rt.FeedMessage()
    m.header.gtfs_realtime_version = "2.0"
    m.header.incrementality = rt.FeedHeader.FULL_DATASET
    m.header.timestamp = ts
    for u in updates:
        e = m.entity.add()
        e.id = u["id"]
        tu = e.trip_update
        tu.trip.trip_id, tu.trip.start_date = u["trip_id"], u["start_date"]
        if u.get("cancelled"):
            tu.trip.schedule_relationship = rt.TripDescriptor.CANCELED
            continue
        tu.trip.schedule_relationship = (
            rt.TripDescriptor.REPLACEMENT if u.get("detour") else rt.TripDescriptor.SCHEDULED
        )
        for s in u["stop_times"]:
            x = tu.stop_time_update.add()
            x.stop_sequence = s["stop_sequence"]
            if s.get("stop_id"):
                x.stop_id = s["stop_id"]
            if s.get("cancelled"):
                x.schedule_relationship = rt.TripUpdate.StopTimeUpdate.SKIPPED
                continue
            unc = 0 if s.get("confirmed") else 1
            if s.get("arrival"):
                x.arrival.time, x.arrival.uncertainty = s["arrival"], unc
            if s.get("departure"):
                x.departure.time, x.departure.uncertainty = s["departure"], unc
    return m


class TUBuilder:
    def __init__(self, apikey, static_url, cache_dir: Path, static_max_age_h=12):
        self.api = PlkApi(apikey)
        self.static_url, self.cache, self.age = static_url, cache_dir, static_max_age_h
        self.static, self.static_key, self.alt_at = None, None, 0.0
        cache_dir.mkdir(parents=True, exist_ok=True)

    def ensure_static(self):
        today = dt.datetime.now(TZ).date()
        dates = [today + dt.timedelta(days=k) for k in (-2, -1, 0, 1)]  # kursy nocne/wielodniowe
        # Ta sama kopia co dla VehiclePositions: jeden plik, jedno pobranie.
        zp = cached(self.static_url, self.cache / "national.zip", self.age)
        key = (today, zp.stat().st_mtime)
        if key != self.static_key:
            self.static, self.static_key, self.alt_at = PlkStatic(zp, dates), key, 0.0
        if time.time() - self.alt_at >= ALT_LOOKUP_PERIOD_S:
            try:
                self.reload_alt(today)
            except RateLimited:
                raise
            except Exception as ex:  # brak tablicy zapasowej nie blokuje TU
                log.warning("tablica zapasowa (schedules/shortened): %r", ex)
            self.alt_at = time.time()

    def reload_alt(self, today):
        r = self.api.schedules_shortened(today - dt.timedelta(days=1), today + dt.timedelta(days=1))
        alt = {}
        for t in r.get("rt") or []:
            num = t.get("nn") or t.get("idn") or t.get("ian")
            if not num:
                continue
            cc = (t.get("cc") or "").strip()
            ag = CARRIER_TO_AGENCY.get(cc, cc)
            for od in t.get("od") or []:
                d = ymd(od)
                alt[(t.get("sid"), t.get("oid"), d)] = (ag, num, d)
        self.static.alt = alt
        log.info("tablica zapasowa: %d pozycji", len(alt))

    def run_once(self, out_path: Path):
        self.ensure_static()
        req0 = self.api.requests
        ts, trains = self.api.operations()
        stats = defaultdict(int)
        updates = build_updates(self.static, trains, stats)
        now = int(time.time())
        feed = to_feed(updates, parse_local(ts) if ts else now)
        write_feed(feed, out_path)
        info = {
            "trains": len(trains),
            "entities": len(feed.entity),
            "requests": self.api.requests - req0,
            "limits": dict(self.api.limits),
        }
        log.info(
            "TU: %d pociągów z API -> %d encji (%d zapytań, limity %s)",
            len(trains),
            len(feed.entity),
            info["requests"],
            self.api.limits,
        )
        log.info("  %s", dict(stats))
        return feed, info
