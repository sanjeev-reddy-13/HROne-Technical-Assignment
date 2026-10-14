"""API behaviour tests on an in-memory fake Mongo (mongomock). Covers CRUD/status codes/rules.
Analytics pipelines need a REAL MongoDB: use extras/verify_live.py for those.   pytest extras/test_api_mock.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone
import mongomock, pytest
from fastapi.testclient import TestClient
from app import main as m

IST = m.IST
def ms(y, mo, d, h, mi, s=0, extra_ms=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + extra_ms

@pytest.fixture()
def c(monkeypatch):
    fake = mongomock.MongoClient(tz_aware=True)["t"]
    monkeypatch.setattr(m, "db", fake)
    m.ensure_indexes()
    return TestClient(m.app)

def emp(c, code="EMP0001", **kw):
    body = {"emp_code": code, "name": "A", "email": "a@x.com", "department": "Eng", "joined_on": "2026-01-05", **kw}
    return c.post("/employees", json=body)

def test_root_points_to_docs_and_health(c):
    r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Employee Attendance Console" in r.text
    assert "/docs" in r.text and "/health" in r.text

def test_employee_crud_and_validation(c):
    r = emp(c); assert r.status_code == 201
    j = r.json(); assert j["shift_start"] == "09:30" and isinstance(j["created_at"], int) and "_id" not in j
    assert emp(c).status_code == 409
    for bad in [dict(emp_code="X1"), dict(emp_code="EMP12"), dict(emp_code="EMP0002\n"), dict(email="nope"),
                dict(shift_start="9:30"), dict(shift_start="25:00"), dict(joined_on="2026-02-30"),
                dict(shift_start="10:00", shift_end="10:00"), dict(name="")]:
        code = bad.pop("emp_code", "EMP0009")
        assert emp(c, code, **bad).status_code == 422, (code, bad)
    emp(c, "EMP0002", department="HR")
    r = c.get("/employees?page_size=1&page=2"); j = r.json()
    assert j["total"] == 2 and [i["emp_code"] for i in j["items"]] == ["EMP0002"]
    assert c.get("/employees?department=HR").json()["total"] == 1
    assert c.get("/employees?page=0").status_code == 422 and c.get("/employees?page_size=101").status_code == 422

def test_punch_flow_and_rules(c):
    emp(c)
    assert c.post("/attendance/punch-in", json={"emp_code": "NOPE"}).status_code == 404
    for bad in [{"emp_code": "EMP0001", "punched_at": 1783312500},            # seconds
                {"emp_code": "EMP0001", "punched_at": 1783312500000.5},       # float
                {"emp_code": "EMP0001", "punched_at": "1783312500000"},       # string
                {"emp_code": "EMP0001", "punched_at": True},
                {"emp_code": "EMP0001", "status": "ABSENT"}, {}]:
        assert c.post("/attendance/punch-in", json=bad).status_code == 422, bad
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,6,9,40,1,900)})
    assert r.status_code == 201
    j = r.json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 10 and j["punch_in"] == ms(2026,7,6,9,40,1) and j["history"] == []
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,6,11,0)}).status_code == 409
    # punch-out validations
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,6,9,40,1)}).status_code == 422
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,7,9,40,2)}).status_code == 422
    assert c.post("/attendance/punch-out", json={"emp_code": "NOPE"}).status_code == 404
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,6,19,10)})
    j = r.json(); assert r.status_code == 200
    assert j["overtime_minutes"] == 40 and j["work_hours"] == 9.5 and j["half_day"] is False
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,6,19,20)}).status_code == 409

def test_overnight_punch_out_across_midnight(c):
    emp(c, "EMP0005", shift_start="22:00", shift_end="06:00")
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026,7,6,21,55)}); assert r.status_code == 201
    assert r.json()["date"] == "2026-07-06"
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026,7,7,6,40)})
    j = r.json(); assert r.status_code == 200 and j["work_hours"] == 8.75 and j["overtime_minutes"] == 40

def test_list_attendance(c):
    emp(c); emp(c, "EMP0002")
    for code in ("EMP0001", "EMP0002"):
        for d in (6, 7, 8):
            c.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2026,7,d,9,0)})
    j = c.get("/attendance").json()
    assert j["total"] == 6 and [(i["date"], i["emp_code"]) for i in j["items"]][:3] == [("2026-07-08","EMP0001"),("2026-07-08","EMP0002"),("2026-07-07","EMP0001")]
    assert c.get("/attendance?date_from=2026-07-07&date_to=2026-07-07").json()["total"] == 2
    assert c.get("/attendance?emp_code=EMP0002&page_size=2&page=2").json()["items"][0]["date"] == "2026-07-06"
    assert c.get("/attendance?date_from=2026-07-09&date_to=2026-07-01").status_code == 422
    assert c.get("/attendance?date_from=bad").status_code == 422
    assert c.get("/attendance?status=NOPE").status_code == 422

def test_regularize(c):
    emp(c)
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,7,10,5)})
    c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,7,18,30)})
    url = "/attendance/EMP0001/2026-07-07"
    base = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    r = c.patch(url, json={**base, "punch_in": ms(2026,7,7,9,28), "work_hours": 99})   # derived field ignored
    j = r.json(); assert r.status_code == 200
    assert j["late_minutes"] == 0 and j["work_hours"] == 9.03 and len(j["history"]) == 1
    ch = j["history"][0]["changes"]
    assert set(ch) == {"punch_in", "late_minutes", "work_hours"}, ch
    assert ch["late_minutes"] == {"from": 35, "to": 0} and ch["punch_in"]["from"] == ms(2026,7,7,10,5)
    assert c.patch(url, json={**base, "punch_in": ms(2026,7,7,9,28)}).status_code == 422          # nothing changes
    assert c.patch(url, json={**base, "status": "ABSENT", "punch_in": ms(2026,7,7,9,0)}).status_code == 422
    assert c.patch(url, json={**base, "punch_in": ms(2026,7,8,9,0)}).status_code == 422           # other date
    assert c.patch(url, json={**base, "punch_out": ms(2026,7,7,9,0)}).status_code == 422          # before punch_in
    assert c.patch(url, json={"reason": "x", "regularized_by": "a", "status": "WFH"}).status_code == 422
    assert c.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "WFH"}).status_code == 404
    assert c.patch("/attendance/EMP9999/2026-07-07", json={**base, "status": "WFH"}).status_code == 404
    assert c.patch("/attendance/EMP0001/2026-13-07", json={**base, "status": "WFH"}).status_code == 422
    r = c.patch(url, json={**base, "status": "LEAVE"}); j = r.json()
    assert r.status_code == 200 and j["punch_in"] is None and j["work_hours"] is None and len(j["history"]) == 2
    r = c.patch(url, json={**base, "status": "PRESENT"}); assert r.status_code == 422             # needs punch_in
    r = c.patch(url, json={**base, "status": "ON_DUTY", "punch_in": ms(2026,7,7,9,0)}); assert r.status_code == 200
    assert r.json()["status"] == "ON_DUTY" and len(r.json()["history"]) == 3

def test_legacy_record_without_history(c):
    emp(c, "EMP0002")
    m.db.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
        "punch_in": datetime(2026,7,14,4,2,tzinfo=timezone.utc), "punch_out": datetime(2026,7,14,13,1,tzinfo=timezone.utc),
        "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    j = c.get("/attendance?emp_code=EMP0002").json()["items"][0]
    assert j["history"] == [] and j["half_day"] is False
    r = c.patch("/attendance/EMP0002/2026-07-14", json={"reason": "fix punch", "regularized_by": "hr", "punch_in": ms(2026,7,14,9,0)})
    assert r.status_code == 200 and len(r.json()["history"]) == 1
