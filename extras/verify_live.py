"""
End-to-end verification against a REAL MongoDB and a RUNNING server.

  1) Start MongoDB (e.g. docker run -d -p 27017:27017 mongo:7)
  2) export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_verify
  3) uvicorn app.main:app --port 8000        (in another terminal, same env vars)
  4) python extras/verify_live.py            (pip install httpx pymongo first)

It WIPES the two collections in MONGO_DB (never point it at data you care about), seeds ~100k
attendance records full of awkward cases, then compares every analytics endpoint against a slow
brute-force Python reference, tests concurrency, and checks the explain plans.
"""
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import httpx
from pymongo import MongoClient

BASE = os.getenv("BASE_URL", "http://localhost:8000")
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_verify")]
http = httpx.Client(base_url=BASE, timeout=60)
random.seed(7)
PRES = {"PRESENT", "WFH", "ON_DUTY"}
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))
    fails += 0 if ok else 1


def hu(num, den, places):  # exact half-up reference using Decimal
    q = Decimal(1).scaleb(-places)
    return float((Decimal(num) / Decimal(den)).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Seed
# --------------------------------------------------------------------------- #
def seed():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    plan = [("Eng", 300), ("HR", 100), ("Ops", 200), ("Support", 90), ("Bench", 10)]
    emps, n = [], 0
    for dept, count in plan:
        for _ in range(count):
            n += 1
            joined = "2026-01-01"
            r = random.random()
            if r < 0.04:
                joined = "2026-07-15"      # mid-month joiner (July)
            elif r < 0.06:
                joined = "2026-08-10"      # joins after July
            emps.append({"emp_code": f"EMP{n:04d}", "name": f"Person {n}", "email": f"p{n}@x.com",
                         "department": dept, "joined_on": joined,
                         "shift_start": "22:00" if dept == "Ops" and n % 2 else "09:30",
                         "shift_end": "06:00" if dept == "Ops" and n % 2 else "18:30",
                         "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    db.employees.insert_many([dict(e) for e in emps])
    logs = []
    d0, d1 = date(2025, 10, 1), date(2026, 7, 31)
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    for e in emps:
        if e["department"] == "Bench":
            continue  # employees with NO logs at all
        for d in days:
            weekend = d.weekday() >= 5
            if (weekend and random.random() > 0.02) or (not weekend and random.random() < 0.2):
                continue  # missing weekdays are "no record" (different from ABSENT)
            r = random.random()
            status = ("PRESENT" if r < .76 else "WFH" if r < .84 else "ON_DUTY" if r < .88
                      else "LEAVE" if r < .92 else "ABSENT")
            doc = {"emp_code": e["emp_code"], "date": d.isoformat(), "status": status, "punch_in": None,
                   "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                   "half_day": False, "history": []}
            if status in PRES:
                pin = datetime(d.year, d.month, d.day, 4, 0, tzinfo=timezone.utc)
                doc["punch_in"] = pin
                doc["late_minutes"] = random.choice([0, 0, 0, 0, 0, 11, 15, 15, 20, 30, 45, 60])  # many ties
                if random.random() < 0.9:
                    wh = round(random.uniform(2.0, 11.0), 2)
                    if random.random() < 0.05:
                        wh = random.choice([4.49, 4.5, 4.51, 8.12, 8.13])  # half-up rounding traps
                    doc["punch_out"] = pin + timedelta(hours=wh)
                    doc["work_hours"] = wh
                    doc["half_day"] = wh < 4.5
                    doc["overtime_minutes"] = random.choice([0, 0, 0, 30, 45, 75])
            if random.random() < 0.01:  # legacy shape
                doc.pop("half_day"); doc.pop("history")
            logs.append(doc)
    for i in range(30):  # logs of unknown employees must be ignored
        logs.append({"emp_code": "EMP9990", "date": f"2026-07-{i + 1:02d}", "status": "PRESENT",
                     "punch_in": datetime(2026, 7, 1, 4, tzinfo=timezone.utc), "punch_out": None,
                     "work_hours": 9.0, "late_minutes": 99, "overtime_minutes": 0, "half_day": False})
    for i in range(0, len(logs), 20000):
        db.attendance_logs.insert_many(logs[i:i + 20000])
    print(f"seeded {len(emps)} employees, {len(logs)} attendance records")
    return emps


# --------------------------------------------------------------------------- #
# Brute-force reference implementations
# --------------------------------------------------------------------------- #
def month_days(month):
    y, m = int(month[:4]), int(month[5:])
    d = date(y, m, 1)
    out = []
    while d.month == m:
        out.append(d); d += timedelta(days=1)
    return out


def weight(l):  # present weight in halves, weekday only
    if l["status"] not in PRES or date.fromisoformat(l["date"]).weekday() >= 5:
        return 0
    return 1 if l.get("half_day", False) else 2


def ref_monthly(emp, month, logs):
    ls = [l for l in logs if l["emp_code"] == emp["emp_code"] and l["date"].startswith(month)]
    wd = sum(1 for d in month_days(month) if d.weekday() < 5 and d >= date.fromisoformat(emp["joined_on"]))
    halves = sum(weight(l) for l in ls)
    return {"emp_code": emp["emp_code"], "month": month, "working_days": wd, "present_days": halves / 2,
            "leave_days": sum(l["status"] == "LEAVE" for l in ls),
            "late_count": sum(l.get("late_minutes", 0) > 0 for l in ls),
            "total_late_minutes": sum(l.get("late_minutes", 0) for l in ls),
            "total_overtime_minutes": sum(l.get("overtime_minutes", 0) for l in ls),
            "attendance_pct": hu(halves * 100, 2 * wd, 2) if wd else None}


def ref_summary(month, emps, logs, department=None):
    end = month_days(month)[-1].isoformat()
    by = {}
    for e in emps:
        if e["joined_on"] <= end and (department is None or e["department"] == department):
            by.setdefault(e["department"], []).append(e["emp_code"])
    items = []
    for dep in sorted(by):
        codes = set(by[dep])
        ls = [l for l in logs if l["emp_code"] in codes and l["date"].startswith(month)]
        hrs = [l["work_hours"] for l in ls if l["status"] in PRES and l.get("work_hours") is not None]
        micro = sum(round(h * 1_000_000) for h in hrs)
        items.append({"department": dep, "headcount": len(codes), "present_days": sum(weight(l) for l in ls) / 2,
                      "avg_work_hours": hu(micro, len(hrs) * 1_000_000, 2) if hrs else None,
                      "late_count": sum(l.get("late_minutes", 0) > 0 for l in ls),
                      "total_late_minutes": sum(l.get("late_minutes", 0) for l in ls),
                      "leave_count": sum(l["status"] == "LEAVE" for l in ls),
                      "on_duty_count": sum(l["status"] == "ON_DUTY" for l in ls)})
    return {"month": month, "items": items}


def ref_leaderboard(month, limit, emps, logs, department=None):
    emap = {e["emp_code"]: e for e in emps}
    tot = {}
    for l in logs:
        if l["date"].startswith(month) and l["emp_code"] in emap and l.get("late_minutes", 0) > 0:
            if department is None or emap[l["emp_code"]]["department"] == department:
                t = tot.setdefault(l["emp_code"], [0, 0]); t[0] += l["late_minutes"]; t[1] += 1
    rows = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0]))
    out = []
    for i, (code, (mins, cnt)) in enumerate(rows):
        rank = 1 + sum(1 for _, (m2, _) in rows if m2 > mins)
        if rank <= limit:
            out.append({"rank": rank, "emp_code": code, "name": emap[code]["name"],
                        "department": emap[code]["department"], "total_late_minutes": mins, "late_count": cnt})
    return {"month": month, "items": out}


def ref_trend(dep, d_from, d_to, emps, logs):
    mine = [e for e in emps if e["department"] == dep]
    codes = {e["emp_code"] for e in mine}
    by_day = {}
    for l in logs:
        if l["emp_code"] in codes and d_from.isoformat() <= l["date"] <= d_to.isoformat():
            by_day.setdefault(l["date"], []).append(l)
    items, rates = [], []
    d = d_from
    while d <= d_to:
        ds = d.isoformat()
        ls = by_day.get(ds, [])
        hc = sum(e["joined_on"] <= ds for e in mine)
        ph = sum(1 if l.get("half_day", False) else 2 for l in ls if l["status"] in PRES)
        working = d.weekday() < 5
        rate = hu(ph, 2 * hc, 4) if working and hc > 0 else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        ma = hu(round(sum(Decimal(str(r)) for r in window) * 10000), len(window) * 10000, 4) if window else None
        items.append({"date": ds, "is_working_day": working, "headcount": hc, "present_count": ph / 2,
                      "late_count": sum(l.get("late_minutes", 0) > 0 for l in ls),
                      "attendance_rate": rate, "moving_avg_7d": ma})
        d += timedelta(days=1)
    return {"department": dep, "items": items}


def get(path, **params):
    r = http.get(path, params=params)
    return r.status_code, (r.json() if r.content else None)


def same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def first_diff(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a:
            if k in b and not same(a[k], b[k]):
                return f"{k}: " + first_diff(a[k], b[k])
        return f"keys {set(a) ^ set(b)}"
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"len {len(a)} vs {len(b)}"
        for x, y in zip(a, b):
            if not same(x, y):
                return first_diff(x, y)
    return f"got {a!r} expected {b!r}"


# --------------------------------------------------------------------------- #
def main():
    s, _ = get("/health")
    if s != 200:
        sys.exit("server not healthy at " + BASE)
    emps = seed()
    logs = list(db.attendance_logs.find({}, {"_id": 0}))

    print("\n== monthly ==")
    sample = random.sample(emps, 25) + [e for e in emps if e["joined_on"] == "2026-07-15"][:3] \
        + [e for e in emps if e["department"] == "Bench"][:2] + [e for e in emps if e["joined_on"] == "2026-08-10"][:2]
    for e in sample:
        for month in ("2026-07", "2026-02"):
            s, got = get(f"/analytics/employees/{e['emp_code']}/monthly", month=month)
            exp = ref_monthly(e, month, logs)
            check(f"monthly {e['emp_code']} {month}", s == 200 and same(got, exp), first_diff(got, exp) if s == 200 else s)
    check("monthly unknown employee -> 404", get("/analytics/employees/EMP8888/monthly", month="2026-07")[0] == 404)
    check("monthly bad month -> 422", get("/analytics/employees/EMP0001/monthly", month="2026-13")[0] == 422)

    print("\n== department summary ==")
    for month in ("2026-07", "2026-06", "2026-02", "2025-11"):
        s, got = get("/analytics/departments/summary", month=month)
        exp = ref_summary(month, emps, logs)
        check(f"summary {month}", s == 200 and same(got, exp), first_diff(got, exp) if s == 200 else s)
    s, got = get("/analytics/departments/summary", month="2026-07", department="Bench")
    exp = ref_summary("2026-07", emps, logs, "Bench")
    check("summary Bench (no logs: headcount>0, avg null)", same(got, exp) and got["items"][0]["avg_work_hours"] is None, str(got))

    print("\n== leaderboard ==")
    for month in ("2026-07", "2026-05"):
        for limit in (1, 3, 10, 50):
            for dep in (None, "Eng", "HR"):
                params = {"month": month, "limit": limit, **({"department": dep} if dep else {})}
                s, got = get("/analytics/leaderboard/late", **params)
                exp = ref_leaderboard(month, limit, emps, logs, dep)
                check(f"leaderboard {params}", s == 200 and same(got, exp), first_diff(got, exp) if s == 200 else s)
    s, got = get("/analytics/leaderboard/late", month="2026-07", limit=5)
    ranks = [r["rank"] for r in got["items"]]
    print("      sample ranks:", ranks, "(ties share a rank; rank may skip)")

    print("\n== trend ==")
    for dep, a, b in [("Eng", date(2026, 7, 1), date(2026, 7, 31)), ("HR", date(2026, 6, 25), date(2026, 7, 20)),
                      ("Bench", date(2026, 7, 1), date(2026, 7, 14)), ("Support", date(2026, 7, 4), date(2026, 7, 4)),
                      ("Ops", date(2026, 5, 1), date(2026, 7, 31)), ("Eng", date(2030, 1, 1), date(2030, 1, 10))]:
        s, got = get(f"/analytics/departments/{dep}/trend", **{"from": a.isoformat(), "to": b.isoformat()})
        exp = ref_trend(dep, a, b, emps, logs)
        check(f"trend {dep} {a}..{b} ({(b - a).days + 1} rows)", s == 200 and same(got, exp), first_diff(got, exp) if s == 200 else s)
    check("trend unknown dept -> 404", get("/analytics/departments/Nope/trend", **{"from": "2026-07-01", "to": "2026-07-05"})[0] == 404)
    check("trend to<from -> 422", get("/analytics/departments/Eng/trend", **{"from": "2026-07-05", "to": "2026-07-01"})[0] == 422)
    check("trend 93 days -> 422", get("/analytics/departments/Eng/trend", **{"from": "2026-01-01", "to": "2026-04-03"})[0] == 422)
    check("trend 92 days -> 200", get("/analytics/departments/Eng/trend", **{"from": "2026-01-01", "to": "2026-04-02"})[0] == 200)

    print("\n== explain (needs IXSCAN, no COLLSCAN) ==")
    cases = {"attendance_list": {"emp_code": "EMP0001", "date_from": "2026-06-01", "date_to": "2026-07-31"},
             "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"},
             "department_summary": {"month": "2026-07"},
             "late_leaderboard": {"month": "2026-07", "limit": 5},
             "department_trend": {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"}}
    extra = [("attendance_list", {}), ("attendance_list", {"status": "ABSENT"}),
             ("attendance_list", {"date_from": "2026-07-01"}), ("department_summary", {"month": "2026-07", "department": "Eng"})]
    for name, params in list(cases.items()) + extra:
        t = time.time()
        r = http.get(f"/admin/explain/{name}", params=params)
        text = r.text
        check(f"explain {name} {params}", r.status_code == 200 and "IXSCAN" in text and "COLLSCAN" not in text,
              f"status {r.status_code} IXSCAN={'IXSCAN' in text} COLLSCAN={'COLLSCAN' in text}")
    check("explain missing params -> 422", http.get("/admin/explain/employee_monthly").status_code == 422)

    print("\n== speed at ~%d records ==" % db.attendance_logs.estimated_document_count())
    for label, path, params in [("list all", "/attendance", {}), ("list status filter", "/attendance", {"status": "LEAVE", "page": 3}),
                                ("list emp", "/attendance", {"emp_code": "EMP0010"}),
                                ("summary", "/analytics/departments/summary", {"month": "2026-07"}),
                                ("leaderboard", "/analytics/leaderboard/late", {"month": "2026-07"}),
                                ("trend 92d", "/analytics/departments/Eng/trend", {"from": "2026-05-01", "to": "2026-07-31"})]:
        t = time.time(); r = http.get(path, params=params); dt = time.time() - t
        check(f"{label}: {dt * 1000:.0f} ms", r.status_code == 200 and dt < 2.0, f"{dt:.2f}s")

    print("\n== concurrency ==")
    def post(path, body):
        return http.post(path, json=body).status_code

    for i in range(3):
        code = f"EMP{7000 + i}"
        with ThreadPoolExecutor(20) as ex:
            res = list(ex.map(lambda _: post("/employees", {"emp_code": code, "name": "Race", "email": "r@x.com",
                                                             "department": "Race", "joined_on": "2026-01-01"}), range(20)))
        check(f"20 parallel employee creates -> one 201 ({code})", sorted(res).count(201) == 1 and res.count(409) == 19, str(sorted(set(res))))
    base = int(datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc).timestamp()) * 1000
    with ThreadPoolExecutor(20) as ex:
        res = list(ex.map(lambda _: post("/attendance/punch-in", {"emp_code": "EMP7000", "punched_at": base}), range(20)))
    check("20 parallel punch-ins -> exactly one 201, rest 409", res.count(201) == 1 and res.count(409) == 19, str(res))
    with ThreadPoolExecutor(20) as ex:
        res = list(ex.map(lambda _: post("/attendance/punch-out", {"emp_code": "EMP7000", "punched_at": base + 9 * 3600 * 1000}), range(20)))
    check("20 parallel punch-outs -> exactly one 200", res.count(200) == 1 and res.count(409) == 19, str(res))
    n = 12
    def patch(i):
        return http.patch("/attendance/EMP7000/2026-07-06", json={"reason": f"edit number {i}", "regularized_by": "t",
                          "punch_out": base + (9 * 3600 + 60 * (i + 1)) * 1000}).status_code
    with ThreadPoolExecutor(n) as ex:
        res = list(ex.map(patch, range(n)))
    rec = http.get("/attendance", params={"emp_code": "EMP7000"}).json()["items"][0]
    check("concurrent PATCHes: history entries == successful PATCHes", len(rec["history"]) == res.count(200),
          f"{len(rec['history'])} entries vs statuses {res}")

    print(f"\n{'ALL CHECKS PASSED' if not fails else str(fails) + ' CHECK(S) FAILED'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
