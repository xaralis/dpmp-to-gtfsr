"""Compares a freshly built feed against the last one built from the old API.

Not a test. Some differences are expected -- the migration deliberately gives
up per-platform coordinates, and the timetable itself moved on between the two
builds -- so this reports rather than asserts. The point is that a human can
see at a glance whether anything changed that should not have.
"""

import csv
import datetime as dt
import io
import sys
import zipfile
from collections import defaultdict

KEYED = {
    "stops.txt": "stop_id",
    "routes.txt": "route_id",
    "trips.txt": "trip_id",
}


def read(path: str, name: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as z, z.open(name) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf8")))


def stop_times_by_trip(path: str) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in read(path, "stop_times.txt"):
        out[row["trip_id"]].append((row["stop_id"], row["departure_time"]))
    return out


def compare_keyed(old: str, new: str, name: str, key: str) -> None:
    a = {r[key]: r for r in read(old, name)}
    b = {r[key]: r for r in read(new, name)}
    print(f"\n=== {name} ===")
    print(f"  old {len(a)}, new {len(b)}")
    only_old, only_new = sorted(a.keys() - b.keys()), sorted(b.keys() - a.keys())
    print(f"  jen ve starém: {len(only_old)} {only_old[:10]}")
    print(f"  jen v novém  : {len(only_new)} {only_new[:10]}")

    changed: dict[str, int] = defaultdict(int)
    for k in a.keys() & b.keys():
        for field in a[k]:
            if field in b[k] and a[k][field] != b[k][field]:
                changed[field] += 1
    for field, count in sorted(changed.items(), key=lambda kv: -kv[1]):
        note = ""
        if name == "stops.txt" and field in ("stop_lat", "stop_lon"):
            note = "  <- OČEKÁVANÉ: nástupiště dědí bod stanice"
        print(f"  změněno {field}: {count}{note}")


def compare_stop_times(old: str, new: str) -> None:
    a, b = stop_times_by_trip(old), stop_times_by_trip(new)
    shared = a.keys() & b.keys()
    same = sum(1 for t in shared if a[t] == b[t])
    print("\n=== stop_times.txt ===")
    print(f"  společných spojů: {len(shared)}")
    print(f"  identická sekvence i časy: {same}")
    differing = [t for t in sorted(shared) if a[t] != b[t]]
    print(f"  odlišných: {len(differing)} {differing[:10]}")
    for trip in differing[:3]:
        print(f"    {trip}\n      starý {a[trip][:4]}\n      nový  {b[trip][:4]}")


def compare_directions(old: str, new: str) -> None:
    """Compare the *partition* into directions, not the labels.

    ``direction_id`` is arbitrary in GTFS, and the two feeds derive it in
    completely different ways: the old one from the CIS pattern name, this one
    from the order trips visit their shared stops. Whether a given line's 0 and
    1 come out swapped says nothing. What would matter is a line where the
    grouping itself disagrees -- two trips that used to run the same way now
    split apart -- so that is what this counts.
    """
    a = {r["trip_id"]: r for r in read(old, "trips.txt")}
    b = {r["trip_id"]: r for r in read(new, "trips.txt")}

    by_route: dict[str, list[str]] = defaultdict(list)
    for tid in a.keys() & b.keys():
        by_route[b[tid]["route_id"]].append(tid)

    print("\n=== direction_id (rozdělení, ne popisky) ===")
    flipped, clean, broken = [], [], []
    for route, trips in sorted(by_route.items()):
        agree = sum(1 for t in trips if a[t]["direction_id"] == b[t]["direction_id"])
        if agree == len(trips):
            clean.append(route)
        elif agree == 0:
            flipped.append(route)
        else:
            broken.append((route, len(trips) - max(agree, len(trips) - agree), len(trips)))

    print(f"  linky beze změny            : {len(clean)}")
    print(f"  linky celé prohozené 0<->1  : {len(flipped)} {flipped}")
    print("      (očekávané: popisek směru je v GTFS libovolný, záleží jen na rozdělení)")
    print(f"  linky s rozpadlým rozdělením: {len(broken)}")
    for route, odd, total in broken:
        print(f"    {route}: {odd} z {total} spojů nesedí ani do jedné varianty <- VYSVĚTLIT")


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def running_days(path: str) -> dict[str, frozenset[dt.date]]:
    """The actual dates each trip runs, from ``calendar.txt`` *and*
    ``calendar_dates.txt``.

    Reading only the weekday columns would misread this feed badly: services
    whose days come from CIS as a bitmap can have an empty weekly pattern and
    carry every day they run as an exception, and those would come out looking
    like trips that never run at all.
    """
    weekly: dict[str, tuple[dt.date, dt.date, set[int]]] = {}
    for row in read(path, "calendar.txt"):
        weekly[row["service_id"]] = (
            dt.date.fromisoformat(row["start_date"]),
            dt.date.fromisoformat(row["end_date"]),
            {i for i, day in enumerate(WEEKDAYS) if row[day] == "1"},
        )

    dates: dict[str, set[dt.date]] = {}
    for service_id, (start, end, days) in weekly.items():
        span = (end - start).days
        dates[service_id] = {
            day
            for day in (start + dt.timedelta(days=n) for n in range(span + 1))
            if day.weekday() in days
        }

    for row in read(path, "calendar_dates.txt"):
        day = dt.date.fromisoformat(row["date"])
        on = dates.setdefault(row["service_id"], set())
        on.add(day) if row["exception_type"] == "1" else on.discard(day)

    return {
        r["trip_id"]: frozenset(dates.get(r["service_id"], ())) for r in read(path, "trips.txt")
    }


def overlap(old: str, new: str) -> tuple[dt.date, dt.date]:
    """The dates both feeds claim to describe.

    Outside it one of them simply has nothing to say, and counting that as a
    difference would report the gap between two build dates as a timetable
    change.
    """
    spans = []
    for path in (old, new):
        info = read(path, "feed_info.txt")[0]
        spans.append(
            (
                dt.date.fromisoformat(info["feed_start_date"]),
                dt.date.fromisoformat(info["feed_end_date"]),
            )
        )
    return max(s for s, _ in spans), min(e for _, e in spans)


def compare_calendars(old: str, new: str) -> None:
    """Whether a trip actually changed the days it runs.

    Comparing ``service_id`` would only report renames, and the two feeds name
    services differently by design. Comparing dates is the question worth
    asking, restricted to the window both feeds cover -- outside it one of them
    simply has nothing to say.
    """
    a, b = running_days(old), running_days(new)
    shared = a.keys() & b.keys()
    if not shared:
        print("\n=== dny provozu ===\n  žádné společné spoje")
        return

    start, end = overlap(old, new)
    window = frozenset(
        start + dt.timedelta(days=n) for n in range((end - start).days + 1) if end >= start
    )
    differing = sorted(t for t in shared if (a[t] & window) != (b[t] & window))
    print("\n=== dny provozu (v překryvu platnosti obou feedů) ===")
    print(f"  společných spojů: {len(shared)}")
    print(f"  stejné dny      : {len(shared) - len(differing)}")
    print(f"  jiné dny        : {len(differing)} {differing[:10]}")
    for trip in differing[:5]:
        gained = [str(day) for day in sorted((b[trip] - a[trip]) & window)[:3]]
        lost = [str(day) for day in sorted((a[trip] - b[trip]) & window)[:3]]
        print(f"    {trip}: přibylo {gained}, ubylo {lost}")


def main() -> None:
    old, new = sys.argv[1], sys.argv[2]
    for name, key in KEYED.items():
        compare_keyed(old, new, name, key)
    compare_stop_times(old, new)
    compare_directions(old, new)
    compare_calendars(old, new)


if __name__ == "__main__":
    main()
