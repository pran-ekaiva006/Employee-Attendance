"""The six sample employees and nine sample attendance documents from DATA_MODEL.md, as stored in MongoDB."""
from datetime import datetime, timezone


def utc(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _employee(code, name, email, department, joined_on, created_at, shift_start="09:30", shift_end="18:30"):
    return {
        "emp_code": code,
        "name": name,
        "email": email,
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on,
        "created_at": created_at,
    }


EMPLOYEES = [
    _employee("EMP0001", "Asha Rao", "asha@example.com", "Engineering", "2026-01-05", utc(2026, 1, 5, 4, 30)),
    _employee("EMP0002", "Vikram Shah", "vikram@example.com", "Engineering", "2026-07-13", utc(2026, 7, 13, 4, 0)),
    _employee("EMP0003", "Neha Iyer", "neha@example.com", "Sales", "2026-02-01", utc(2026, 2, 1, 4, 30)),
    _employee("EMP0004", "Rohit Das", "rohit@example.com", "Sales", "2026-02-01", utc(2026, 2, 1, 4, 31)),
    _employee("EMP0005", "Meera Nair", "meera@example.com", "Support", "2026-03-10", utc(2026, 3, 10, 4, 30),
              shift_start="22:00", shift_end="06:00"),
    _employee("EMP0006", "Karan Bedi", "karan@example.com", "Support", "2026-03-10", utc(2026, 3, 10, 4, 31)),
]


def _log(code, date, status, punch_in, punch_out, work_hours, late, overtime, half_day, history=None):
    return {
        "emp_code": code,
        "date": date,
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
        "history": history or [],
    }


SAMPLE_1_NORMAL = _log("EMP0001", "2026-07-06", "PRESENT", utc(2026, 7, 6, 3, 58), utc(2026, 7, 6, 13, 5), 9.12, 0, 0, False)
SAMPLE_2_LATE_OVERTIME = _log("EMP0003", "2026-07-06", "PRESENT", utc(2026, 7, 6, 4, 35), utc(2026, 7, 6, 13, 40), 9.08, 35, 40, False)
SAMPLE_3_OPEN = _log("EMP0006", "2026-07-07", "PRESENT", utc(2026, 7, 7, 4, 5), None, None, 0, 0, False)
SAMPLE_4_HALF_DAY = _log("EMP0004", "2026-07-07", "WFH", utc(2026, 7, 7, 4, 0), utc(2026, 7, 7, 7, 30), 3.5, 0, 0, True)
SAMPLE_5_ABSENT = _log("EMP0004", "2026-07-08", "ABSENT", None, None, None, 0, 0, False)
SAMPLE_6_LEAVE = _log("EMP0003", "2026-07-08", "LEAVE", None, None, None, 0, 0, False)
SAMPLE_7_OVERNIGHT = _log("EMP0005", "2026-07-06", "PRESENT", utc(2026, 7, 6, 16, 25), utc(2026, 7, 7, 1, 10), 8.75, 0, 40, False)
SAMPLE_8_REGULARIZED = _log(
    "EMP0001", "2026-07-07", "PRESENT", utc(2026, 7, 7, 3, 58), utc(2026, 7, 7, 13, 0), 9.03, 0, 0, False,
    history=[
        {
            "at": utc(2026, 7, 8, 5, 0),
            "by": "hr.admin",
            "reason": "biometric glitch",
            "changes": {
                "punch_in": {"from": utc(2026, 7, 7, 4, 35), "to": utc(2026, 7, 7, 3, 58)},
                "late_minutes": {"from": 35, "to": 0},
                "work_hours": {"from": 8.42, "to": 9.03},
            },
        }
    ],
)
SAMPLE_9_LEGACY = {
    "emp_code": "EMP0002",
    "date": "2026-07-14",
    "status": "PRESENT",
    "punch_in": utc(2026, 7, 14, 4, 2),
    "punch_out": utc(2026, 7, 14, 13, 1),
    "work_hours": 8.98,
    "late_minutes": 0,
    "overtime_minutes": 0,
}

ATTENDANCE_LOGS = [
    SAMPLE_1_NORMAL,
    SAMPLE_2_LATE_OVERTIME,
    SAMPLE_3_OPEN,
    SAMPLE_4_HALF_DAY,
    SAMPLE_5_ABSENT,
    SAMPLE_6_LEAVE,
    SAMPLE_7_OVERNIGHT,
    SAMPLE_8_REGULARIZED,
    SAMPLE_9_LEGACY,
]
