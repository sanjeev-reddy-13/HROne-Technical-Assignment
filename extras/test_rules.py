"""Unit tests for the pure business rules (no MongoDB needed):  pytest extras/test_rules.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone, timedelta
import pytest
from app import main as m

IST = m.IST
DAY = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}

def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)

# R2 examples straight from the contract (shift 09:30)
@pytest.mark.parametrize("h,mi,s,expected", [(9,40,0,0),(9,40,1,10),(10,15,59,45),(9,0,0,0),(9,39,59,0),(9,41,0,11)])
def test_late(h, mi, s, expected):
    assert m.compute_late_minutes(ist(2026,7,6,h,mi,s), "2026-07-06", DAY) == expected

@pytest.mark.parametrize("out_h,out_m,out_s,expected", [(19,0,0,30),(18,59,59,0),(19,10,0,40),(18,0,0,0),(19,0,59,30)])
def test_overtime(out_h,out_m,out_s,expected):
    assert m.compute_overtime(ist(2026,7,6,out_h,out_m,out_s), "2026-07-06", DAY) == expected

def test_overtime_overnight():
    # sample 7: shift 22:00-06:00, out 06:40 next day -> 40
    assert m.compute_overtime(ist(2026,7,7,6,40), "2026-07-06", NIGHT) == 40

def test_work_hours_half_up():
    pi = ist(2026,7,6,9,0)
    assert m.compute_work_hours(pi, pi + timedelta(seconds=54)) == 0.02     # 0.015 -> half-up (round() gives 0.01)
    assert m.compute_work_hours(pi, pi + timedelta(seconds=18)) == 0.01     # 0.005
    assert m.compute_work_hours(pi, pi + timedelta(hours=9, minutes=7)) == 9.12  # sample 1
    assert m.compute_work_hours(ist(2026,7,6,21,55), ist(2026,7,7,6,40)) == 8.75  # sample 7

def test_half_day_uses_rounded_value():
    pi = ist(2026,7,6,9,0)
    # 4.495h -> 4.50 (rounded) -> NOT a half day
    f = m.derive_fields("PRESENT", pi, pi + timedelta(seconds=int(4.495*3600)), "2026-07-06", DAY)
    assert f["work_hours"] == 4.5 and f["half_day"] is False
    f = m.derive_fields("WFH", pi, pi + timedelta(hours=3, minutes=30), "2026-07-06", DAY)
    assert f["work_hours"] == 3.5 and f["half_day"] is True

def test_overnight_date_rule():
    assert m.attendance_date(ist(2026,7,6,21,55), NIGHT) == "2026-07-06"
    assert m.attendance_date(ist(2026,7,7,5,59,59), NIGHT) == "2026-07-06"   # before shift_end -> previous day
    assert m.attendance_date(ist(2026,7,7,6,0,0), NIGHT) == "2026-07-07"     # not earlier than shift_end
    assert m.attendance_date(ist(2026,7,7,0,30), DAY) == "2026-07-07"
    # IST date, not UTC date: 00:30 IST is still the previous day in UTC
    assert m.attendance_date(ist(2026,7,7,0,30), DAY) == "2026-07-07"

def test_late_after_midnight_for_overnight_shift():
    # punch-in 00:30 IST belongs to the previous day's 22:00 shift => 150 min late
    pi = ist(2026,7,7,0,30)
    day = m.attendance_date(pi, NIGHT)
    assert m.compute_late_minutes(pi, day, NIGHT) == 150

def test_ms_roundtrip_and_truncation():
    assert m.ms_from_dt(m.dt_from_ms(1783312500999)) == 1783312500000
    naive = datetime(2026,7,6,4,35)
    assert m.ms_from_dt(naive) == m.ms_from_dt(naive.replace(tzinfo=timezone.utc))

def test_div_half_up():
    assert m.div_half_up(1, 32, 4) == 0.0313   # 0.03125 -> 0.0313 (banker's would give 0.0312)
    assert m.div_half_up(25, 200, 2) == 0.13   # 0.125
    assert m.div_half_up(0, 5, 2) == 0.0

def test_working_days():
    from datetime import date
    f, l = date(2026,7,1), date(2026,7,31)
    assert m.count_working_days(f, l, date(2026,1,1)) == 23
    assert m.count_working_days(f, l, date(2026,7,15)) == 13   # mid-month joiner (15th..31st)
    assert m.count_working_days(f, l, date(2026,8,1)) == 0
