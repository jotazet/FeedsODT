"""GTFS-RT kolei (feedsodt/realtime): TripUpdates z API PLK i dopasowanie pozycji.

Bez sieci: krajowy GTFS to mała sztuczna paczka w tmp_path, odpowiedzi API to słowniki
w kształcie /operations/shortened.
"""

import datetime as dt
import zipfile
from collections import defaultdict

import pytest
from google.transit import gtfs_realtime_pb2 as rt

from feedsodt.realtime import loop
from feedsodt.realtime.plk import PlkStatic, build_updates, expand_number, numbers_of, to_feed
from feedsodt.realtime.vehicles import (
    TZ,
    National,
    day_base,
    match_by_id,
    match_fallback,
    train_numbers,
)

DAY = dt.date(2026, 11, 18)
D = "20261118"


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text.strip() + "\n")
    return path


@pytest.fixture
def national_zip(tmp_path):
    """Trzy kursy KS 1234 (dziś i wczoraj) i 1236 na linii A-B-C, jeden IC po północy."""
    return _zip(
        tmp_path / "polish_trains.zip",
        {
            "feed_info.txt": """
feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date,feed_version
x,https://example.invalid,pl,20261101,20261231,v1
""",
            "agency.txt": """
agency_id,agency_name,agency_url,agency_timezone
KS,Koleje Śląskie,https://example.invalid,Europe/Warsaw
IC,PKP Intercity,https://example.invalid,Europe/Warsaw
""",
            "routes.txt": """
route_id,agency_id,route_short_name,route_type
R1,KS,S1,2
R2,IC,IC,2
""",
            "stops.txt": """
stop_id,stop_name,stop_lat,stop_lon
1000,A,50.00,19.00
2000,B,50.10,19.10
3000,C,50.20,19.20
4000,D,51.00,20.00
""",
            "calendar.txt": """
service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
""",
            "calendar_dates.txt": """
service_id,date,exception_type
PLK_1,20261118,1
PLK_1,20261117,1
PLK_2+1D,20261119,1
""",
            "trips.txt": """
route_id,service_id,trip_id,trip_short_name,plk_train_number
R1,PLK_1,PLK_KS_100_1,1234,
R1,PLK_1,PLK_KS_101_1,1236,
R2,PLK_2+1D,PLK_IC_200_2,5678,
""",
            "stop_times.txt": """
trip_id,arrival_time,departure_time,stop_id,stop_sequence,plk_sequence,platform,track
PLK_KS_100_1,08:00:00,08:00:00,1000,1,1,1,2
PLK_KS_100_1,08:30:00,08:31:00,2000,2,2,,
PLK_KS_100_1,09:00:00,09:00:00,3000,3,3,,
PLK_KS_101_1,10:00:00,10:00:00,1000,1,1,,
PLK_KS_101_1,10:30:00,10:31:00,2000,2,2,,
PLK_KS_101_1,11:00:00,11:00:00,3000,3,3,,
PLK_IC_200_2,00:10:00,00:10:00,1000,1,1,,
PLK_IC_200_2,00:40:00,00:40:00,2000,2,2,,
""",
        },
    )


# ───────────────────────── numery pociągów ─────────────────────────


def test_train_numbers():
    assert expand_number("69304/5") == ["69304", "69305"]
    assert numbers_of({"trip_short_name": "11276 Kwisa", "plk_train_number": "11276/7"}) == [
        "11276",
        "11277",
    ]
    assert train_numbers("69304/5 Kwisa") == {69304, 69305}
    assert train_numbers("11276/11277") == {11276, 11277}
    assert train_numbers("Kwisa") == set()


# ───────────────────────── TripUpdates ─────────────────────────


def _plk_static(national_zip):
    return PlkStatic(national_zip, [DAY - dt.timedelta(days=1), DAY])


def test_build_updates_maps_times_skipped_stops_and_confirmation(national_zip):
    static = _plk_static(national_zip)
    stats = defaultdict(int)
    trains = [
        {
            "od": "2026-11-18",
            "sid": 100,
            "oid": 1,
            "st": [
                {"psn": 1, "asn": 1, "id": 1000, "ad": "2026-11-18T08:02:00", "cf": True},
                {"psn": 2, "asn": 2, "id": 2000, "cn": True},
                {"psn": 3, "asn": 3, "id": 3000, "aa": "2026-11-18T09:05:00+01:00"},
            ],
        }
    ]
    [update] = build_updates(static, trains, stats)
    assert update["trip_id"] == "PLK_KS_100_1" and update["start_date"] == D
    feed = to_feed([update], 1)
    stu = feed.entity[0].trip_update.stop_time_update
    assert [s.stop_sequence for s in stu] == [1, 2, 3]
    assert stu[0].departure.time == int(dt.datetime(2026, 11, 18, 8, 2, tzinfo=TZ).timestamp())
    assert stu[0].departure.uncertainty == 0  # potwierdzony
    assert stu[1].schedule_relationship == rt.TripUpdate.StopTimeUpdate.SKIPPED
    assert stu[2].arrival.uncertainty == 1  # prognoza
    assert stats["dopasowane (id)"] == 1


def test_build_updates_day_offset_cancellation_and_unmatched(national_zip):
    static = _plk_static(national_zip)
    stats = defaultdict(int)
    trains = [
        # Kurs PLK z 18.11, który w GTFS jest usługą 19.11 (PLK_2+1D).
        {"od": "2026-11-18", "sid": 200, "oid": 2, "st": [], "s": "X"},
        {"od": "2026-11-18", "sid": 999, "oid": 9, "st": []},
        {"od": "2027-03-01", "sid": 100, "oid": 1, "st": []},
    ]
    [update] = build_updates(static, trains, stats)
    assert update["cancelled"] and update["start_date"] == "20261119"
    feed = to_feed([update], 1)
    assert feed.entity[0].trip_update.trip.schedule_relationship == rt.TripDescriptor.CANCELED
    assert stats["niedopasowane"] == 1 and stats["poza zakresem dat GTFS"] == 1


def test_build_updates_detour_uses_fallback_stops(national_zip):
    static = _plk_static(national_zip)
    stats = defaultdict(int)
    trains = [
        {
            "od": "2026-11-18",
            "sid": 100,
            "oid": 1,
            "st": [
                {"psn": 1, "asn": 1, "id": 1000, "ad": "2026-11-18T08:00:00"},
                {"psn": 2, "asn": 2, "id": 4000, "aa": "2026-11-18T09:30:00"},
            ],
        }
    ]
    [update] = build_updates(static, trains, stats)
    feed = to_feed([update], 1)
    tu = feed.entity[0].trip_update
    assert tu.trip.schedule_relationship == rt.TripDescriptor.REPLACEMENT
    assert [s.stop_id for s in tu.stop_time_update] == ["1000_FALLBACK", "4000_FALLBACK"]
    assert stats["objazd"] == 1


def test_build_updates_never_lets_time_go_back(national_zip):
    static = _plk_static(national_zip)
    stats = defaultdict(int)
    trains = [
        {
            "od": "2026-11-18",
            "sid": 100,
            "oid": 1,
            "st": [
                {"psn": 1, "asn": 1, "id": 1000, "ad": "2026-11-18T08:00:00", "cf": True},
                # Przyjazd opóźniony, odjazd został z rozkładu (przed przyjazdem).
                {
                    "psn": 2,
                    "asn": 2,
                    "id": 2000,
                    "aa": "2026-11-18T08:40:00",
                    "ad": "2026-11-18T08:31:00",
                    "cf": True,
                },
                {"psn": 3, "asn": 3, "id": 3000, "aa": "2026-11-18T08:35:00"},
            ],
        }
    ]
    [update] = build_updates(static, trains, stats)
    stu = to_feed([update], 1).entity[0].trip_update.stop_time_update
    at = int(dt.datetime(2026, 11, 18, 8, 40, tzinfo=TZ).timestamp())
    assert stu[1].arrival.time == stu[1].departure.time == stu[2].arrival.time == at
    assert stu[1].departure.uncertainty == 1  # już nie potwierdzony
    assert stu[0].departure.uncertainty == 0  # czasów w porządku nie rusza
    assert stats["czasy cofnięte -> poprawione"] == 2


def test_build_updates_matches_by_train_number(national_zip):
    static = _plk_static(national_zip)
    # /schedules/shortened powiązał nieznane (sid, oid) z numerem 1234.
    static.alt[(555, 5, DAY)] = ("KS", "1234", DAY)
    stats = defaultdict(int)
    [update] = build_updates(
        static,
        [{"od": "2026-11-18", "sid": 555, "oid": 5, "st": [{"psn": 1, "asn": 1, "id": 1000}]}],
        stats,
    )
    assert update["trip_id"] == "PLK_KS_100_1"
    assert stats["dopasowane (numer)"] == 1


# ───────────────────────── VehiclePositions ─────────────────────────


def _at(hh, mm, date=D):
    return day_base(date) + hh * 3600 + mm * 60


def _vehicle(trip_id, lat, lon):
    v = rt.VehiclePosition()
    v.trip.trip_id = trip_id
    v.position.latitude, v.position.longitude = lat, lon
    return v


class _SourceStatic:
    def __init__(self, nums, stops=None):
        self.nums, self.stops = nums, stops or {}


def _national(national_zip):
    return National(national_zip, {"KS"}, [str(int(D) - 1), D])


def test_match_by_id_picks_the_trip_running_now(national_zip):
    nat = _national(national_zip)
    src = _SourceStatic({"rt-1": {1234}})
    v = _vehicle("rt-1", 50.05, 19.05)
    # Ten sam numer jedzie wczoraj i dziś: wygrywa kurs w oknie czasowym.
    (match, how, dist), err = match_by_id(nat, src, "KS", v, _at(8, 15), None)
    assert err is None and match == ("PLK_KS_100_1", D)
    assert dist < 1


def test_match_by_id_refuses_a_vehicle_long_after_its_trip(national_zip):
    nat = _national(national_zip)
    src = _SourceStatic({"rt-1": {1234}})
    res, err = match_by_id(nat, src, "KS", _vehicle("rt-1", 50.2, 19.2), _at(12, 0), None)
    assert res is None and "okn" in err


def test_match_by_id_skips_cancelled_trips(national_zip):
    nat = _national(national_zip)
    nat.canceled = {("PLK_KS_100_1", D)}
    src = _SourceStatic({"rt-1": {1234}})
    res, err = match_by_id(nat, src, "KS", _vehicle("rt-1", 50.05, 19.05), _at(8, 15), None)
    assert res is None


def test_match_fallback_publishes_only_an_unambiguous_trip(national_zip):
    nat = _national(national_zip)
    # Bez czasów TU: jeden kurs w pobliżu o 08:15 -> dopasowany po pozycji.
    (match, how, _), err = match_fallback(nat, "KS", _vehicle("x", 50.05, 19.05), _at(8, 15), [])
    assert match == ("PLK_KS_100_1", D) and how == "fallback:pozycja"
    # Daleko od każdego kursu -> nic.
    res, err = match_fallback(nat, "KS", _vehicle("x", 52.0, 21.0), _at(8, 15), [])
    assert res is None


def test_match_fallback_by_times(national_zip):
    nat = _national(national_zip)
    times = [_at(8, 0), _at(8, 30), _at(8, 31)]
    (match, how, _), err = match_fallback(nat, "KS", _vehicle("x", 50.05, 19.05), _at(8, 15), times)
    assert match == ("PLK_KS_100_1", D) and how.startswith("fallback:czasy")


# ───────────────────────── pętla ─────────────────────────


def test_tu_wait_spreads_the_daily_limit_until_midnight():
    now = dt.datetime(2026, 11, 18, 12, 0, tzinfo=TZ)
    plenty = {"limits": {"Daily-Remaining": 20000}, "requests": 3}
    assert loop.next_tu_wait(plenty, 60, now) == 60
    # 12 h do północy, 120 zapytań po 3 -> 40 przebiegów: co 18 minut.
    scarce = {"limits": {"Daily-Remaining": 120}, "requests": 3}
    assert loop.next_tu_wait(scarce, 60, now) == pytest.approx(12 * 3600 / 40)


def test_failure_wait_backs_off_and_waits_for_key_activation():
    assert loop.failure_wait(RuntimeError("HTTP 500"), 0) == (1, 60)
    assert loop.failure_wait(RuntimeError("HTTP 500"), 6) == (6, 30 * 64)
    assert loop.failure_wait(RuntimeError("HTTP 403 PendingActivation"), 3) == (0, 300)


def test_shared_tu_expires():
    shared = loop.SharedTU()
    feed = rt.FeedMessage()
    shared.put(feed)
    assert shared.fresh() is feed
    assert shared.fresh(now=shared.at + loop.TU_MAX_AGE_S + 1) is None
