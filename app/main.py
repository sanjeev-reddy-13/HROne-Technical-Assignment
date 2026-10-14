"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Layout of this file (everything lives here on purpose):
  1. Configuration and database handle
  2. Time helpers (epoch ms <-> datetime, IST)
  3. Business rules R1-R5 (pure functions, no database)
  4. Serializers (Mongo document -> API JSON)
  5. Request models and validation helpers
  6. Startup: indexes
  7. Employees, attendance endpoints
  8. Analytics (pipeline builders + endpoints)
  9. Admin explain
"""
import json
import logging
import os
import random
import re
import time
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, StrictInt, StrictStr, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # does NOT override variables that are already set in the environment

log = logging.getLogger("attendance")

# The fallbacks only exist for local development; the grader injects its own values.
MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB") or "attendance_db"

# tz_aware=True: PyMongo returns timezone-aware (UTC) datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
NON_PRESENCE_STATUSES = ("ABSENT", "LEAVE")
ALL_STATUSES = PRESENCE_STATUSES + NON_PRESENCE_STATUSES

GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_BELOW_HOURS = 4.5       # R5
MAX_SHIFT_SECONDS = 24 * 3600    # punch-out / correction window
MAX_TREND_DAYS = 92
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


# --------------------------------------------------------------------------- #
# 2. Time helpers
# --------------------------------------------------------------------------- #
def as_utc(dt: datetime) -> datetime:
    """Naive datetimes (e.g. from a non tz-aware client) are treated as UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def dt_from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def ms_from_dt(dt: datetime) -> int:
    """Aware/naive datetime -> epoch milliseconds using exact integer arithmetic."""
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def ms_or_none(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else ms_from_dt(dt)


def now_dt() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def div_half_up(numerator: int, denominator: int, places: int) -> float:
    """numerator/denominator rounded HALF-UP to `places` decimals, using integers only
    (no float error, and not banker's rounding). Both arguments must be non-negative."""
    scale = 10 ** places
    rounded = (2 * numerator * scale + denominator) // (2 * denominator)
    return rounded / scale


def parse_date_or_422(value: str, name: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value or ""):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid calendar date")


def month_bounds(month: str) -> tuple[date, date]:
    """First and last day of a YYYY-MM string."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise HTTPException(422, "month year must be >= 0001")
    return date(year, mon, 1), date(year, mon, monthrange(year, mon)[1])


def weekend_days(first: date, last: date) -> list[str]:
    out, d = [], first
    while d <= last:
        if d.weekday() >= 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def count_working_days(first: date, last: date, joined_on: date) -> int:
    """R7: Mon-Fri between max(first, joined_on) and last."""
    d, n = max(first, joined_on), 0
    while d <= last:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# 3. Business rules R1-R5 (pure functions)
# --------------------------------------------------------------------------- #
def hhmm_to_seconds(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def is_overnight(emp: dict) -> bool:
    return hhmm_to_seconds(emp["shift_end"]) <= hhmm_to_seconds(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; for overnight shifts a punch-in earlier
    than shift_end belongs to the shift that started the previous day."""
    local = as_utc(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(emp):
        seconds_into_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_into_day < hhmm_to_seconds(emp["shift_end"]):
            day -= timedelta(days=1)
    return day.isoformat()


def _ist_at(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    d = date.fromisoformat(date_str) + timedelta(days=plus_days)
    s = hhmm_to_seconds(hhmm)
    return datetime(d.year, d.month, d.day, s // 3600, (s % 3600) // 60, tzinfo=IST)


def shift_start_at(date_str: str, emp: dict) -> datetime:
    return _ist_at(date_str, emp["shift_start"])


def shift_end_at(date_str: str, emp: dict) -> datetime:
    """The shift end of an overnight shift falls on the next calendar day (R1)."""
    return _ist_at(date_str, emp["shift_end"], 1 if is_overnight(emp) else 0)


def compute_late_minutes(punch_in: datetime, date_str: str, emp: dict) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then floor(minutes
    since shift_start)."""
    delta = int((as_utc(punch_in) - shift_start_at(date_str, emp)).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals."""
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return div_half_up(seconds, 3600, 2)


def compute_overtime(punch_out: datetime, date_str: str, emp: dict) -> int:
    """R3: whole minutes after shift_end, only if at least 30."""
    delta = int((as_utc(punch_out) - shift_end_at(date_str, emp)).total_seconds())
    minutes = delta // 60  # floor
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  date_str: str, emp: dict) -> dict:
    """The four server-computed fields, from the final status and punch times.
    Single source of truth for punch-in, punch-out and regularization."""
    if status in NON_PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {"work_hours": None, "late_minutes": compute_late_minutes(punch_in, date_str, emp),
           "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, emp)
        out["half_day"] = out["work_hours"] < HALF_DAY_BELOW_HOURS  # R5 uses the ROUNDED value
    return out


# --------------------------------------------------------------------------- #
# 4. Serializers
# --------------------------------------------------------------------------- #
def _api_value(v: Any) -> Any:
    return ms_from_dt(v) if isinstance(v, datetime) else v


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc.get("name"), "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start", "09:30"), "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc.get("joined_on"), "created_at": ms_or_none(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    changes = {k: {"from": _api_value(v.get("from")), "to": _api_value(v.get("to"))}
               for k, v in (entry.get("changes") or {}).items()}
    return {"at": ms_or_none(entry.get("at")), "by": entry.get("by"),
            "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Missing legacy fields: history -> [], half_day -> false, counters -> 0."""
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": ms_or_none(doc.get("punch_in")), "punch_out": ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 5. Request models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]  # rejects floats, strings, bools


class EmployeeIn(BaseModel):
    emp_code: StrictStr
    name: StrictStr = Field(min_length=1, max_length=100)
    email: StrictStr = Field(max_length=120)
    department: StrictStr = Field(min_length=1, max_length=50)
    shift_start: StrictStr = "09:30"
    shift_end: StrictStr = "18:30"
    joined_on: StrictStr

    @field_validator("emp_code")
    @classmethod
    def _emp_code(cls, v: str) -> str:
        if not re.fullmatch(r"EMP[0-9]{4,6}", v):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("invalid email")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", v):
            raise ValueError("must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def _joined(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
            raise ValueError("must be YYYY-MM-DD")
        date.fromisoformat(v)  # ValueError -> 422 for e.g. 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: StrictStr
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: StrictStr
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Derived fields (work_hours, ...) are not declared, so a client cannot set them: ignored.
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: StrictStr = Field(min_length=5, max_length=200)
    regularized_by: StrictStr = Field(min_length=1, max_length=50)


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 6. Startup: indexes (idempotent) and app object
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    """create_index is a no-op when the same index already exists, so this is idempotent."""
    global _indexes_ready
    specs = [
        # employees
        (db.employees, [("emp_code", ASCENDING)], {"unique": True}),            # identity + 409 on duplicates
        (db.employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {}),  # dept filter, sorted by emp_code
        (db.employees, [("joined_on", ASCENDING)], {}),                          # headcount (R9) without department
        # attendance_logs
        (db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),  # one record/day, race-safe
        (db.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {}),   # list sort + month/day range scans
        (db.attendance_logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {}),  # status filter
    ]
    for coll, keys, opts in specs:
        try:
            coll.create_index(keys, **opts)
        except PyMongoError as exc:  # do not crash the app; /health reports MongoDB problems
            log.error("index creation failed on %s %s: %s", coll.name, keys, exc)
            return
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  # MongoDB may still be starting; /health retries
        log.error("startup index creation failed: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    log.error("MongoDB error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 7a. System + employees
# --------------------------------------------------------------------------- #
FRONTEND_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Employee Attendance Console</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #17212b;
      --muted: #647284;
      --line: #d9e0e7;
      --accent: #0f766e;
      --accent-strong: #0b5f59;
      --warn: #b45309;
      --bad: #b42318;
      --good: #157347;
      --chip: #edf7f5;
      --shadow: 0 12px 30px rgba(15, 23, 42, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input, select, textarea { font: inherit; }
    button {
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: white;
      min-height: 36px;
      padding: 0 12px;
      cursor: pointer;
      font-weight: 650;
    }
    button:hover { background: var(--accent-strong); }
    button.secondary {
      background: #eef2f6;
      color: var(--ink);
      border: 1px solid var(--line);
    }
    button.secondary:hover { background: #e4eaf0; }
    button.danger { background: var(--bad); }
    a { color: var(--accent-strong); text-decoration: none; }
    header {
      min-height: 76px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 14px 24px;
      background: #ffffff;
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .brand { display: flex; gap: 12px; align-items: center; min-width: 260px; }
    .mark {
      width: 42px;
      height: 42px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background: var(--accent);
      color: white;
      font-weight: 800;
    }
    h1 { font-size: 18px; margin: 0; letter-spacing: 0; }
    .subtle { color: var(--muted); font-size: 12px; }
    .statusbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    .pill {
      display: inline-flex;
      gap: 7px;
      align-items: center;
      min-height: 30px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: white;
      padding: 0 10px;
      color: var(--muted);
      white-space: nowrap;
    }
    .dot { width: 8px; height: 8px; border-radius: 999px; background: var(--warn); }
    .dot.ok { background: var(--good); }
    main { max-width: 1440px; margin: 0 auto; padding: 18px 20px 40px; }
    .tabs {
      display: flex;
      gap: 6px;
      overflow-x: auto;
      padding: 4px 0 16px;
    }
    .tab {
      background: transparent;
      color: var(--muted);
      border: 1px solid transparent;
      white-space: nowrap;
    }
    .tab.active {
      background: var(--panel);
      color: var(--ink);
      border-color: var(--line);
      box-shadow: var(--shadow);
    }
    .view { display: none; }
    .view.active { display: block; }
    .grid {
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 14px;
      align-items: start;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-5 { grid-column: span 5; }
    .span-6 { grid-column: span 6; }
    .span-7 { grid-column: span 7; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }
    .panel-head {
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
    }
    h2 { font-size: 15px; margin: 0; letter-spacing: 0; }
    .panel-body { padding: 16px; }
    .form-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; font-weight: 650; }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      min-height: 36px;
      padding: 7px 9px;
      background: white;
      color: var(--ink);
    }
    textarea { min-height: 80px; resize: vertical; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; align-items: center; }
    .table-wrap { overflow: auto; max-height: 560px; }
    table { width: 100%; border-collapse: collapse; min-width: 720px; }
    th, td {
      text-align: left;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      white-space: nowrap;
    }
    th { background: #f8fafc; color: var(--muted); font-size: 12px; position: sticky; top: 0; z-index: 1; }
    tr:hover td { background: #fbfcfd; }
    .metric-row {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfd;
      min-height: 82px;
    }
    .metric b { display: block; font-size: 24px; margin-top: 4px; }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 0 8px;
      background: var(--chip);
      color: var(--accent-strong);
      font-weight: 700;
      font-size: 12px;
    }
    .badge.absent { background: #fff1f0; color: var(--bad); }
    .badge.leave { background: #fff7ed; color: var(--warn); }
    pre {
      margin: 0;
      background: #101820;
      color: #eaf2f8;
      border-radius: 8px;
      padding: 12px;
      overflow: auto;
      max-height: 420px;
      white-space: pre-wrap;
    }
    .toast {
      position: fixed;
      right: 18px;
      bottom: 18px;
      max-width: 460px;
      background: #101820;
      color: white;
      border-radius: 8px;
      padding: 12px 14px;
      box-shadow: var(--shadow);
      display: none;
      z-index: 10;
    }
    .toast.show { display: block; }
    canvas {
      width: 100%;
      height: 260px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
    }
    @media (max-width: 980px) {
      header { align-items: flex-start; flex-direction: column; gap: 12px; }
      .statusbar { justify-content: flex-start; }
      .span-3, .span-4, .span-5, .span-6, .span-7, .span-8 { grid-column: span 12; }
      .metric-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 620px) {
      main { padding: 14px 12px 32px; }
      .form-grid { grid-template-columns: 1fr; }
      .metric-row { grid-template-columns: 1fr; }
      header { padding: 12px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="mark">HR</div>
      <div>
        <h1>Employee Attendance Console</h1>
        <div class="subtle">FastAPI + MongoDB, IST attendance rules</div>
      </div>
    </div>
    <div class="statusbar">
      <span class="pill"><span id="healthDot" class="dot"></span><span id="healthText">Checking MongoDB</span></span>
      <span class="pill">API <a href="/docs" target="_blank" rel="noreferrer">Docs</a></span>
      <span class="pill">OpenAPI <a href="/openapi.json" target="_blank" rel="noreferrer">JSON</a></span>
    </div>
  </header>
  <main>
    <nav class="tabs" aria-label="Sections">
      <button class="tab active" data-tab="dashboard">Dashboard</button>
      <button class="tab" data-tab="employees">Employees</button>
      <button class="tab" data-tab="punch">Punch Desk</button>
      <button class="tab" data-tab="attendance">Attendance</button>
      <button class="tab" data-tab="regularize">Regularize</button>
      <button class="tab" data-tab="analytics">Analytics</button>
      <button class="tab" data-tab="explain">Explain</button>
    </nav>

    <section id="dashboard" class="view active">
      <div class="metric-row">
        <div class="metric"><span class="subtle">Employees</span><b id="mEmployees">-</b></div>
        <div class="metric"><span class="subtle">Attendance Records</span><b id="mAttendance">-</b></div>
        <div class="metric"><span class="subtle">Open Punches</span><b id="mOpen">-</b></div>
        <div class="metric"><span class="subtle">Late Records</span><b id="mLate">-</b></div>
      </div>
      <div class="grid">
        <div class="panel span-7">
          <div class="panel-head"><h2>Recent Attendance</h2><button class="secondary" id="refreshDashboard">Refresh</button></div>
          <div class="table-wrap"><table><thead><tr><th>Date</th><th>Employee</th><th>Status</th><th>In</th><th>Out</th><th>Late</th><th>Hours</th></tr></thead><tbody id="recentRows"></tbody></table></div>
        </div>
        <div class="panel span-5">
          <div class="panel-head"><h2>Quick Analytics</h2></div>
          <div class="panel-body">
            <div class="form-grid">
              <label>Month<input id="dashMonth" type="month"></label>
              <label>Department<input id="dashDepartment" placeholder="Engineering"></label>
            </div>
            <div class="actions"><button id="loadDashSummary">Load Summary</button></div>
            <pre id="dashOutput">{}</pre>
          </div>
        </div>
      </div>
    </section>

    <section id="employees" class="view">
      <div class="grid">
        <div class="panel span-5">
          <div class="panel-head"><h2>Create Employee</h2></div>
          <div class="panel-body">
            <div class="form-grid">
              <label>Employee Code<input id="empCode" placeholder="EMP0007"></label>
              <label>Name<input id="empName" placeholder="Priya Menon"></label>
              <label>Email<input id="empEmail" placeholder="priya@example.com"></label>
              <label>Department<input id="empDept" placeholder="Engineering"></label>
              <label>Shift Start<input id="empStart" value="09:30" placeholder="09:30"></label>
              <label>Shift End<input id="empEnd" value="18:30" placeholder="18:30"></label>
              <label>Joined On<input id="empJoined" type="date"></label>
            </div>
            <div class="actions"><button id="createEmployee">Create</button><button class="secondary" id="clearEmployee">Clear</button></div>
          </div>
        </div>
        <div class="panel span-7">
          <div class="panel-head">
            <h2>Employees</h2>
            <div class="actions" style="margin:0">
              <input id="empFilterDept" placeholder="Department filter" style="width:180px">
              <button class="secondary" id="loadEmployees">Load</button>
            </div>
          </div>
          <div class="table-wrap"><table><thead><tr><th>Code</th><th>Name</th><th>Email</th><th>Department</th><th>Shift</th><th>Joined</th></tr></thead><tbody id="employeeRows"></tbody></table></div>
        </div>
      </div>
    </section>

    <section id="punch" class="view">
      <div class="grid">
        <div class="panel span-6">
          <div class="panel-head"><h2>Punch In</h2></div>
          <div class="panel-body">
            <div class="form-grid">
              <label>Employee Code<input id="pinCode" placeholder="EMP0001"></label>
              <label>Status<select id="pinStatus"><option>PRESENT</option><option>WFH</option><option>ON_DUTY</option></select></label>
              <label>Punched At<input id="pinAt" type="datetime-local"></label>
            </div>
            <div class="actions"><button id="punchIn">Punch In</button><button class="secondary" data-now="pinAt">Use Current Time</button></div>
          </div>
        </div>
        <div class="panel span-6">
          <div class="panel-head"><h2>Punch Out</h2></div>
          <div class="panel-body">
            <div class="form-grid">
              <label>Employee Code<input id="poutCode" placeholder="EMP0001"></label>
              <label>Punched At<input id="poutAt" type="datetime-local"></label>
            </div>
            <div class="actions"><button id="punchOut">Punch Out</button><button class="secondary" data-now="poutAt">Use Current Time</button></div>
          </div>
        </div>
        <div class="panel span-12">
          <div class="panel-head"><h2>Last Punch Result</h2></div>
          <div class="panel-body"><pre id="punchResult">{}</pre></div>
        </div>
      </div>
    </section>

    <section id="attendance" class="view">
      <div class="panel">
        <div class="panel-head"><h2>Attendance Records</h2></div>
        <div class="panel-body">
          <div class="form-grid">
            <label>Employee Code<input id="attCode"></label>
            <label>Status<select id="attStatus"><option value="">Any</option><option>PRESENT</option><option>WFH</option><option>ON_DUTY</option><option>ABSENT</option><option>LEAVE</option></select></label>
            <label>Date From<input id="attFrom" type="date"></label>
            <label>Date To<input id="attTo" type="date"></label>
            <label>Page<input id="attPage" type="number" min="1" value="1"></label>
            <label>Page Size<input id="attPageSize" type="number" min="1" max="100" value="20"></label>
          </div>
          <div class="actions"><button id="loadAttendance">Load</button><button class="secondary" id="nextAttendance">Next Page</button></div>
        </div>
        <div class="table-wrap"><table><thead><tr><th>Date</th><th>Employee</th><th>Status</th><th>In</th><th>Out</th><th>Hours</th><th>Late</th><th>OT</th><th>Half</th><th>History</th><th></th></tr></thead><tbody id="attendanceRows"></tbody></table></div>
      </div>
    </section>

    <section id="regularize" class="view">
      <div class="grid">
        <div class="panel span-5">
          <div class="panel-head"><h2>Regularize Record</h2></div>
          <div class="panel-body">
            <div class="form-grid">
              <label>Employee Code<input id="regCode" placeholder="EMP0001"></label>
              <label>Attendance Date<input id="regDate" type="date"></label>
              <label>Status<select id="regStatus"><option value="">Unchanged</option><option>PRESENT</option><option>WFH</option><option>ON_DUTY</option><option>ABSENT</option><option>LEAVE</option></select></label>
              <label>Punch In<input id="regIn" type="datetime-local"></label>
              <label>Punch Out<input id="regOut" type="datetime-local"></label>
              <label>Regularized By<input id="regBy" value="hr.admin"></label>
            </div>
            <label style="margin-top:12px">Reason<textarea id="regReason">biometric correction</textarea></label>
            <div class="actions"><button id="regularizeBtn">Apply Correction</button><button class="secondary" id="loadRegRecord">Load Current Record</button></div>
          </div>
        </div>
        <div class="panel span-7">
          <div class="panel-head"><h2>Regularization Result</h2></div>
          <div class="panel-body"><pre id="regResult">{}</pre></div>
        </div>
      </div>
    </section>

    <section id="analytics" class="view">
      <div class="grid">
        <div class="panel span-4">
          <div class="panel-head"><h2>Employee Monthly</h2></div>
          <div class="panel-body">
            <label>Employee Code<input id="amCode" placeholder="EMP0001"></label>
            <label style="margin-top:10px">Month<input id="amMonth" type="month"></label>
            <div class="actions"><button id="loadMonthly">Load</button></div>
          </div>
        </div>
        <div class="panel span-4">
          <div class="panel-head"><h2>Department Summary</h2></div>
          <div class="panel-body">
            <label>Month<input id="sumMonth" type="month"></label>
            <label style="margin-top:10px">Department<input id="sumDept" placeholder="optional"></label>
            <div class="actions"><button id="loadSummary">Load</button></div>
          </div>
        </div>
        <div class="panel span-4">
          <div class="panel-head"><h2>Late Leaderboard</h2></div>
          <div class="panel-body">
            <div class="form-grid">
              <label>Month<input id="leadMonth" type="month"></label>
              <label>Limit<input id="leadLimit" type="number" min="1" max="50" value="10"></label>
            </div>
            <label style="margin-top:10px">Department<input id="leadDept" placeholder="optional"></label>
            <div class="actions"><button id="loadLeaderboard">Load</button></div>
          </div>
        </div>
        <div class="panel span-5">
          <div class="panel-head"><h2>Department Trend</h2></div>
          <div class="panel-body">
            <div class="form-grid">
              <label>Department<input id="trendDept" placeholder="Engineering"></label>
              <label>From<input id="trendFrom" type="date"></label>
              <label>To<input id="trendTo" type="date"></label>
            </div>
            <div class="actions"><button id="loadTrend">Load Trend</button></div>
          </div>
        </div>
        <div class="panel span-7">
          <div class="panel-head"><h2>Trend Chart</h2></div>
          <div class="panel-body"><canvas id="trendCanvas" width="900" height="300"></canvas></div>
        </div>
        <div class="panel span-12">
          <div class="panel-head"><h2>Analytics Output</h2></div>
          <div class="panel-body"><pre id="analyticsOutput">{}</pre></div>
        </div>
      </div>
    </section>

    <section id="explain" class="view">
      <div class="grid">
        <div class="panel span-4">
          <div class="panel-head"><h2>Explain Query</h2></div>
          <div class="panel-body">
            <label>Endpoint<select id="exEndpoint"><option>attendance_list</option><option>employee_monthly</option><option>department_summary</option><option>late_leaderboard</option><option>department_trend</option></select></label>
            <div class="form-grid" style="margin-top:12px">
              <label>Employee Code<input id="exEmp" placeholder="EMP0001"></label>
              <label>Month<input id="exMonth" type="month"></label>
              <label>Department<input id="exDept"></label>
              <label>Limit<input id="exLimit" type="number" min="1" max="50" value="10"></label>
              <label>Date From<input id="exDateFrom" type="date"></label>
              <label>Date To<input id="exDateTo" type="date"></label>
              <label>Status<select id="exStatus"><option value="">Any</option><option>PRESENT</option><option>WFH</option><option>ON_DUTY</option><option>ABSENT</option><option>LEAVE</option></select></label>
              <label>Trend From<input id="exFrom" type="date"></label>
              <label>Trend To<input id="exTo" type="date"></label>
            </div>
            <div class="actions"><button id="loadExplain">Run Explain</button></div>
          </div>
        </div>
        <div class="panel span-8">
          <div class="panel-head"><h2>Explain Output</h2></div>
          <div class="panel-body"><pre id="explainOutput">{}</pre></div>
        </div>
      </div>
    </section>
  </main>
  <div id="toast" class="toast"></div>
  <script>
    const $ = (id) => document.getElementById(id);
    const state = { attendancePage: 1 };

    function todayMonth() {
      const d = new Date();
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    }
    function todayDate() {
      const d = new Date();
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    }
    function localInputValue(d = new Date()) {
      const z = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
      return z.toISOString().slice(0, 16);
    }
    function toEpoch(id) {
      const v = $(id).value;
      return v ? new Date(v).getTime() : undefined;
    }
    function fromEpoch(ms) {
      if (ms === null || ms === undefined) return "";
      return new Date(ms).toLocaleString();
    }
    function month(id) {
      return $(id).value || todayMonth();
    }
    function show(obj, target) {
      $(target).textContent = JSON.stringify(obj, null, 2);
    }
    function toast(msg) {
      const el = $("toast");
      el.textContent = msg;
      el.classList.add("show");
      setTimeout(() => el.classList.remove("show"), 3200);
    }
    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: options.body ? {"Content-Type": "application/json"} : undefined,
        ...options,
      });
      const text = await res.text();
      const data = text ? JSON.parse(text) : null;
      if (!res.ok) {
        const detail = data && data.detail ? JSON.stringify(data.detail) : res.statusText;
        throw new Error(`${res.status} ${detail}`);
      }
      return data;
    }
    function params(obj) {
      const p = new URLSearchParams();
      Object.entries(obj).forEach(([k, v]) => {
        if (v !== undefined && v !== null && v !== "") p.set(k, v);
      });
      const s = p.toString();
      return s ? `?${s}` : "";
    }
    function badge(status) {
      const cls = status === "ABSENT" ? "absent" : status === "LEAVE" ? "leave" : "";
      return `<span class="badge ${cls}">${status}</span>`;
    }
    function esc(v) {
      return String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    }

    document.querySelectorAll(".tab").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
        document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
        btn.classList.add("active");
        $(btn.dataset.tab).classList.add("active");
      });
    });
    document.querySelectorAll("[data-now]").forEach(btn => {
      btn.addEventListener("click", () => { $(btn.dataset.now).value = localInputValue(); });
    });

    async function checkHealth() {
      try {
        await api("/health");
        $("healthDot").classList.add("ok");
        $("healthText").textContent = "MongoDB connected";
      } catch (e) {
        $("healthDot").classList.remove("ok");
        $("healthText").textContent = "MongoDB unavailable";
      }
    }
    async function loadEmployees() {
      try {
        const data = await api("/employees" + params({department: $("empFilterDept").value, page_size: 100}));
        $("employeeRows").innerHTML = data.items.map(e => `<tr><td>${esc(e.emp_code)}</td><td>${esc(e.name)}</td><td>${esc(e.email)}</td><td>${esc(e.department)}</td><td>${esc(e.shift_start)}-${esc(e.shift_end)}</td><td>${esc(e.joined_on)}</td></tr>`).join("");
        $("mEmployees").textContent = data.total;
      } catch (e) { toast(e.message); }
    }
    async function createEmployee() {
      try {
        const body = {
          emp_code: $("empCode").value, name: $("empName").value, email: $("empEmail").value,
          department: $("empDept").value, shift_start: $("empStart").value || "09:30",
          shift_end: $("empEnd").value || "18:30", joined_on: $("empJoined").value
        };
        const data = await api("/employees", {method: "POST", body: JSON.stringify(body)});
        toast(`Created ${data.emp_code}`);
        loadEmployees();
      } catch (e) { toast(e.message); }
    }
    async function punchIn() {
      try {
        const body = {emp_code: $("pinCode").value, status: $("pinStatus").value};
        const at = toEpoch("pinAt");
        if (at) body.punched_at = at;
        const data = await api("/attendance/punch-in", {method: "POST", body: JSON.stringify(body)});
        show(data, "punchResult");
        toast("Punch-in saved");
        loadDashboard();
      } catch (e) { toast(e.message); }
    }
    async function punchOut() {
      try {
        const body = {emp_code: $("poutCode").value};
        const at = toEpoch("poutAt");
        if (at) body.punched_at = at;
        const data = await api("/attendance/punch-out", {method: "POST", body: JSON.stringify(body)});
        show(data, "punchResult");
        toast("Punch-out saved");
        loadDashboard();
      } catch (e) { toast(e.message); }
    }
    async function loadAttendance() {
      try {
        state.attendancePage = Number($("attPage").value || 1);
        const data = await api("/attendance" + params({
          emp_code: $("attCode").value, status: $("attStatus").value,
          date_from: $("attFrom").value, date_to: $("attTo").value,
          page: state.attendancePage, page_size: $("attPageSize").value || 20
        }));
        $("mAttendance").textContent = data.total;
        $("attendanceRows").innerHTML = data.items.map(r => `<tr>
          <td>${esc(r.date)}</td><td>${esc(r.emp_code)}</td><td>${badge(r.status)}</td>
          <td>${esc(fromEpoch(r.punch_in))}</td><td>${esc(fromEpoch(r.punch_out))}</td>
          <td>${r.work_hours ?? ""}</td><td>${r.late_minutes}</td><td>${r.overtime_minutes}</td><td>${r.half_day ? "Yes" : "No"}</td><td>${r.history.length}</td>
          <td><button class="secondary" data-reg="${esc(r.emp_code)}|${esc(r.date)}">Edit</button></td>
        </tr>`).join("");
        document.querySelectorAll("[data-reg]").forEach(b => b.addEventListener("click", () => {
          const [code, date] = b.dataset.reg.split("|");
          $("regCode").value = code; $("regDate").value = date;
          document.querySelector('[data-tab="regularize"]').click();
        }));
      } catch (e) { toast(e.message); }
    }
    async function loadDashboard() {
      try {
        const recent = await api("/attendance?page_size=12");
        $("recentRows").innerHTML = recent.items.map(r => `<tr><td>${esc(r.date)}</td><td>${esc(r.emp_code)}</td><td>${badge(r.status)}</td><td>${esc(fromEpoch(r.punch_in))}</td><td>${esc(fromEpoch(r.punch_out))}</td><td>${r.late_minutes}</td><td>${r.work_hours ?? ""}</td></tr>`).join("");
        $("mAttendance").textContent = recent.total;
        $("mOpen").textContent = recent.items.filter(r => r.punch_in && !r.punch_out).length;
        $("mLate").textContent = recent.items.filter(r => r.late_minutes > 0).length;
        loadEmployees();
      } catch (e) { toast(e.message); }
    }
    async function regularize() {
      try {
        const body = {reason: $("regReason").value, regularized_by: $("regBy").value};
        if ($("regStatus").value) body.status = $("regStatus").value;
        const pin = toEpoch("regIn"), pout = toEpoch("regOut");
        if (pin) body.punch_in = pin;
        if (pout) body.punch_out = pout;
        const data = await api(`/attendance/${encodeURIComponent($("regCode").value)}/${$("regDate").value}`, {method: "PATCH", body: JSON.stringify(body)});
        show(data, "regResult");
        toast("Correction applied");
        loadAttendance();
      } catch (e) { toast(e.message); }
    }
    async function loadRegRecord() {
      try {
        const data = await api("/attendance" + params({emp_code: $("regCode").value, date_from: $("regDate").value, date_to: $("regDate").value}));
        show(data.items[0] || {}, "regResult");
      } catch (e) { toast(e.message); }
    }
    async function loadMonthly() {
      try { show(await api(`/analytics/employees/${encodeURIComponent($("amCode").value)}/monthly` + params({month: month("amMonth")})), "analyticsOutput"); }
      catch (e) { toast(e.message); }
    }
    async function loadSummary(target = "analyticsOutput") {
      try { show(await api("/analytics/departments/summary" + params({month: month(target === "dashOutput" ? "dashMonth" : "sumMonth"), department: target === "dashOutput" ? $("dashDepartment").value : $("sumDept").value})), target); }
      catch (e) { toast(e.message); }
    }
    async function loadLeaderboard() {
      try { show(await api("/analytics/leaderboard/late" + params({month: month("leadMonth"), limit: $("leadLimit").value, department: $("leadDept").value})), "analyticsOutput"); }
      catch (e) { toast(e.message); }
    }
    function drawTrend(items) {
      const canvas = $("trendCanvas"), ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.strokeStyle = "#d9e0e7"; ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const y = 20 + i * 60;
        ctx.beginPath(); ctx.moveTo(40, y); ctx.lineTo(canvas.width - 20, y); ctx.stroke();
      }
      const rates = items.map(x => x.attendance_rate).filter(x => x !== null);
      if (!rates.length) return;
      const max = Math.max(1, ...rates);
      const step = (canvas.width - 70) / Math.max(1, items.length - 1);
      ctx.strokeStyle = "#0f766e"; ctx.lineWidth = 3; ctx.beginPath();
      items.forEach((it, i) => {
        if (it.attendance_rate === null) return;
        const x = 40 + i * step;
        const y = 260 - (it.attendance_rate / max) * 220;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.fillStyle = "#17212b"; ctx.font = "12px system-ui";
      ctx.fillText("attendance_rate", 44, 24);
    }
    async function loadTrend() {
      try {
        const data = await api(`/analytics/departments/${encodeURIComponent($("trendDept").value)}/trend` + params({from: $("trendFrom").value, to: $("trendTo").value}));
        show(data, "analyticsOutput");
        drawTrend(data.items);
      } catch (e) { toast(e.message); }
    }
    async function loadExplain() {
      try {
        const data = await api(`/admin/explain/${$("exEndpoint").value}` + params({
          emp_code: $("exEmp").value, month: $("exMonth").value, department: $("exDept").value,
          limit: $("exLimit").value, date_from: $("exDateFrom").value, date_to: $("exDateTo").value,
          status: $("exStatus").value, from: $("exFrom").value, to: $("exTo").value
        }));
        show(data, "explainOutput");
      } catch (e) { toast(e.message); }
    }

    $("createEmployee").addEventListener("click", createEmployee);
    $("clearEmployee").addEventListener("click", () => ["empCode","empName","empEmail","empDept","empJoined"].forEach(id => $(id).value = ""));
    $("loadEmployees").addEventListener("click", loadEmployees);
    $("punchIn").addEventListener("click", punchIn);
    $("punchOut").addEventListener("click", punchOut);
    $("loadAttendance").addEventListener("click", loadAttendance);
    $("nextAttendance").addEventListener("click", () => { $("attPage").value = Number($("attPage").value || 1) + 1; loadAttendance(); });
    $("regularizeBtn").addEventListener("click", regularize);
    $("loadRegRecord").addEventListener("click", loadRegRecord);
    $("loadMonthly").addEventListener("click", loadMonthly);
    $("loadSummary").addEventListener("click", () => loadSummary("analyticsOutput"));
    $("loadDashSummary").addEventListener("click", () => loadSummary("dashOutput"));
    $("loadLeaderboard").addEventListener("click", loadLeaderboard);
    $("loadTrend").addEventListener("click", loadTrend);
    $("loadExplain").addEventListener("click", loadExplain);
    $("refreshDashboard").addEventListener("click", loadDashboard);

    ["dashMonth","amMonth","sumMonth","leadMonth","exMonth"].forEach(id => $(id).value = todayMonth());
    ["trendFrom","trendTo"].forEach(id => $(id).value = todayDate());
    $("empJoined").value = todayDate();
    checkHealth();
    loadDashboard();
    setInterval(checkHealth, 15000);
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root():
    return FRONTEND_HTML


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


def count_matching(collection, query: dict) -> int:
    """Exact count for a filtered query; metadata count (instant, no scan) when unfiltered."""
    return collection.estimated_document_count() if not query else collection.count_documents(query)


def page_skip(page: int, page_size: int) -> int:
    return min((page - 1) * page_size, 10 ** 12)  # clamp absurd pages; they just return no rows


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    created = now_dt()
    doc["created_at"] = created
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code decides races
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    q: dict = {} if department is None else {"department": department}
    total = count_matching(db.employees, q)
    docs = db.employees.find(q).sort("emp_code", ASCENDING).skip(page_skip(page, page_size)).limit(page_size)
    return {"items": [employee_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 7b. Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = dt_from_ms(body.punched_at) if body.punched_at is not None else now_dt()
    day = attendance_date(ts, emp)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status,
           "punch_in": ts, "punch_out": None, "history": []}
    doc.update(derive_fields(body.status, ts, None, day, emp))
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = dt_from_ms(body.punched_at) if body.punched_at is not None else now_dt()
    for _ in range(5):
        # most recent record whose punch_in is at or before the punch-out (works across midnight)
        rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
                                          sort=[("punch_in", DESCENDING)])
        if rec is None:
            raise HTTPException(404, "no punch-in found")
        if rec.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        p_in = as_utc(rec["punch_in"])
        if ts <= p_in:
            raise HTTPException(422, "punched_at must be after the punch-in")
        if (ts - p_in).total_seconds() > MAX_SHIFT_SECONDS:
            raise HTTPException(422, "punch-out is more than 24 hours after the punch-in")
        derived = derive_fields(rec["status"], p_in, ts, rec["date"], emp)
        # Conditional update: only succeeds while the record is still open and unchanged.
        updated = db.attendance_logs.find_one_and_update(
            {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None,
             "punch_in": rec["punch_in"], "status": rec["status"]},
            {"$set": {"punch_out": ts, "work_hours": derived["work_hours"],
                      "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}},
            return_document=ReturnDocument.AFTER)
        if updated is not None:
            return record_out(updated)
        # lost a race: loop re-reads, which yields 409 if somebody else closed it
    raise HTTPException(409, "record is being modified concurrently, retry")


def attendance_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                     status: Optional[str], page: int, page_size: int) -> dict:
    """Shared by GET /attendance and its explain, so both run the exact same query."""
    d_from = parse_date_or_422(date_from, "date_from") if date_from is not None else None
    d_to = parse_date_or_422(date_to, "date_to") if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return {"filter": q, "sort": {"date": -1, "emp_code": 1},
            "skip": page_skip(page, page_size), "limit": page_size}


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[Status] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    spec = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = count_matching(db.attendance_logs, spec["filter"])
    cursor = (db.attendance_logs.find(spec["filter"])
              .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
              .skip(spec["skip"]).limit(spec["limit"]))
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def _norm_state(status, p_in, p_out, derived) -> dict:
    return {"status": status, "punch_in": p_in, "punch_out": p_out, **derived}


def _same(a, b) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return ms_from_dt(a) == ms_from_dt(b)
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(a - b) < 1e-9
    return a == b


def plan_regularization(rec: dict, emp: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Returns (new field values, changes). Raises 422 for every invalid combination."""
    status = body.status or rec["status"]
    give_in, give_out = body.punch_in is not None, body.punch_out is not None
    if status in NON_PRESENCE_STATUSES:
        if give_in or give_out:
            raise HTTPException(422, f"{status} clears punch times; do not supply punch_in/punch_out")
        p_in = p_out = None
    else:
        stored_in = as_utc(rec["punch_in"]) if rec.get("punch_in") else None
        stored_out = as_utc(rec["punch_out"]) if rec.get("punch_out") else None
        p_in = dt_from_ms(body.punch_in) if give_in else stored_in
        p_out = dt_from_ms(body.punch_out) if give_out else stored_out
        if p_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if give_in and attendance_date(p_in, emp) != rec["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if p_out is not None and not (p_in < p_out <= p_in + timedelta(seconds=MAX_SHIFT_SECONDS)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    new = _norm_state(status, p_in, p_out, derive_fields(status, p_in, p_out, rec["date"], emp))
    old = _norm_state(
        rec["status"], rec.get("punch_in"), rec.get("punch_out"),
        {"work_hours": rec.get("work_hours"), "late_minutes": int(rec.get("late_minutes") or 0),
         "overtime_minutes": int(rec.get("overtime_minutes") or 0), "half_day": bool(rec.get("half_day", False))})
    changes = {k: {"from": old[k], "to": new[k]} for k in new if not _same(old[k], new[k])}
    if not changes:
        raise HTTPException(422, "the request changes nothing")
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date_or_422(date, "date")
    emp = get_employee_or_404(emp_code)
    for attempt in range(8):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = plan_regularization(rec, emp, body)
        entry = {"at": now_dt(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        # Optimistic concurrency: apply only if the record is exactly as we read it
        # (same status, punches and number of history entries). Otherwise re-read and redo,
        # so two concurrent corrections both end up in the history.
        updated = db.attendance_logs.find_one_and_update(
            {"emp_code": emp_code, "date": date, "status": rec["status"],
             "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
             "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]}},
            {"$set": new, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER)
        if updated is not None:
            return record_out(updated)
        time.sleep(random.uniform(0, 0.02))
    raise HTTPException(409, "record was modified concurrently, retry")


# --------------------------------------------------------------------------- #
# 8. Analytics: pipeline builders (shared with the explain endpoint)
# --------------------------------------------------------------------------- #
def _present() -> dict:
    return {"$in": ["$status", list(PRESENCE_STATUSES)]}


def _halves(weekend_list: Optional[list[str]] = None) -> dict:
    """Present-day weight in half-days: 2 = full day, 1 = half day, 0 = not present
    (optionally 0 on weekends). Integers keep every sum exact."""
    conds: list = [_present()]
    if weekend_list is not None:
        conds.append({"$eq": [{"$in": ["$date", weekend_list]}, False]})
    return {"$cond": [{"$and": conds},
                      {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}, 0]}


_LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
_IS_LATE = {"$cond": [{"$gt": [_LATE_MIN, 0]}, 1, 0]}


def _date_range(month: str) -> dict:
    # 'YYYY-MM-31' is above every real day of the month and below the next month: no date maths needed
    return {"$gte": f"{month}-01", "$lte": f"{month}-31"}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": _date_range(month)}},
        {"$group": {
            "_id": None,
            "present_halves": {"$sum": _halves(weekend_days(first, last))},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": _IS_LATE},
            "total_late_minutes": {"$sum": _LATE_MIN},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    emp_filter: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9: joined on/before month end
    if department is not None:
        emp_filter["department"] = department
    has_hours = {"$and": [_present(), {"$isNumber": "$work_hours"}]}
    per_employee = [
        {"$match": {"date": _date_range(month)}},
        {"$group": {
            "_id": None,
            "present_halves": {"$sum": _halves(weekend_days(first, last))},
            "work_micro": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 1000000]}, 0]}, 0]}},
            "work_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
            "late_count": {"$sum": _IS_LATE},
            "total_late_minutes": {"$sum": _LATE_MIN},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    return [
        # Start from EMPLOYEES so people with zero logs still count (R9)...
        {"$match": emp_filter},
        # ...and join each one's monthly log totals (index: attendance_logs emp_code+date).
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "s"}},
        {"$set": {"s": {"$arrayElemAt": ["$s", 0]}}},  # missing when the employee has no logs
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_halves": {"$sum": "$s.present_halves"},
            "work_micro": {"$sum": "$s.work_micro"},
            "work_n": {"$sum": "$s.work_n"},
            "late_count": {"$sum": "$s.late_count"},
            "total_late_minutes": {"$sum": "$s.total_late_minutes"},
            "leave_count": {"$sum": "$s.leave_count"},
            "on_duty_count": {"$sum": "$s.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    month_bounds(month)  # validates the year
    pipeline: list = [
        {"$match": {"date": _date_range(month), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1,2,2,4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def department_trend_pipeline(department: str, emp_codes: list[str], d_from: date, d_to: date) -> list:
    n_days = (d_to - d_from).days + 1
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    day_str = {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}
    window = {"documents": [-6, 0]}  # current row + up to 6 preceding rows, inside the range
    return [
        {"$match": {"date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()},
                    "emp_code": {"$in": emp_codes}}},
        {"$group": {"_id": "$date", "ph": {"$sum": _halves()}, "lc": {"$sum": _IS_LATE}}},
        # $facet always emits exactly one document, even when no log matched. That one document
        # is the seed from which every calendar day of the range is generated (gap filling).
        {"$facet": {"rows": [{"$sort": {"_id": 1}}]}},
        {"$project": {"_id": 0, "rows": 1, "day": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "i",
            "in": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}}}}}},
        {"$unwind": "$day"},
        {"$set": {"date": day_str, "is_working_day": {"$eq": [{"$in": [{"$dayOfWeek": "$day"}, [1, 7]]}, False]}}},
        {"$set": {"row": {"$arrayElemAt": [
            {"$filter": {"input": "$rows", "as": "r", "cond": {"$eq": ["$$r._id", "$date"]}}}, 0]}}},
        {"$set": {"present_halves": {"$ifNull": ["$row.ph", 0]}, "late_count": {"$ifNull": ["$row.lc", 0]}}},
        # R9: headcount on that day = department employees with joined_on <= day
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
            {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        # attendance_rate in units of 1e-4, rounded HALF-UP with integer arithmetic:
        # floor((10000*ph + hc) / (2*hc)) == round_half_up(ph/2/hc * 10000)
        {"$set": {"rate4": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present_halves", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$set": {"has_rate": {"$cond": [{"$eq": ["$rate4", None]}, 0, 1]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "sum4": {"$sum": "$rate4", "window": window},      # $sum ignores nulls
            "cnt": {"$sum": "$has_rate", "window": window}}}},
        # moving average of the non-null (rounded) rates, half-up: floor((2*S + k) / (2*k))
        {"$set": {"ma4": {"$cond": [{"$eq": ["$cnt", 0]}, None,
                                    {"$floor": {"$divide": [{"$add": [{"$multiply": ["$sum4", 2]}, "$cnt"]},
                                                            {"$multiply": ["$cnt", 2]}]}}]}}},
        {"$sort": {"date": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_halves": 1, "late_count": 1, "rate4": 1, "ma4": 1}},
    ]


def trend_inputs(department: str, frm: str, to: str) -> tuple[list[str], date, date]:
    d_from, d_to = parse_date_or_422(frm, "from"), parse_date_or_422(to, "to")
    if d_to < d_from:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, f"range must not exceed {MAX_TREND_DAYS} days")
    codes = [d["emp_code"] for d in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    if not codes:
        raise HTTPException(404, "department not found")
    return codes, d_from, d_to


# --------------------------------------------------------------------------- #
# 8b. Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    agg = next(iter(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month))), None) or {}
    working_days = count_working_days(first, last, date.fromisoformat(emp["joined_on"]))
    halves = int(agg.get("present_halves", 0))
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": halves / 2, "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": div_half_up(halves * 100, 2 * working_days, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(month, department)):
        work_n = int(r["work_n"])
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]),
            "present_days": int(r["present_halves"]) / 2,
            # average over RECORDS: exact integer micro-hours / (records * 1e6), half-up
            "avg_work_hours": div_half_up(int(r["work_micro"]), work_n * 1_000_000, 2) if work_n else None,
            "late_count": int(r["late_count"]), "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=MONTH_PATTERN), limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    rows = db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department))
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])} for r in rows]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    codes, d_from, d_to = trend_inputs(department, from_, to)
    rows = db.attendance_logs.aggregate(department_trend_pipeline(department, codes, d_from, d_to))
    items = []
    for r in rows:
        items.append({
            "date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
            "present_count": int(r["present_halves"]) / 2, "late_count": int(r["late_count"]),
            "attendance_rate": None if r["rate4"] is None else int(r["rate4"]) / 10000,
            "moving_avg_7d": None if r["ma4"] is None else int(r["ma4"]) / 10000,
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 9. Admin: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(**values):
        missing = [k for k, v in values.items() if v is None]
        if missing:
            raise HTTPException(422, f"missing required parameter(s) for {endpoint}: {', '.join(missing)}")

    if endpoint == "attendance_list":
        spec = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, **spec}
    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, month), "cursor": {}}
    elif endpoint == "department_summary":
        need(month=month)
        collection = "employees"  # the pipeline starts from employees and $lookups the logs
        command = {"aggregate": collection, "pipeline": department_summary_pipeline(month, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        need(month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": late_leaderboard_pipeline(month, limit, department), "cursor": {}}
    else:  # department_trend
        need(department=department, **{"from": from_, "to": to})
        codes, d_from, d_to = trend_inputs(department, from_, to)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": department_trend_pipeline(department, codes, d_from, d_to),
                   "cursor": {}}
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    # BSON types (dates, Int64, ...) -> plain JSON
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))}
