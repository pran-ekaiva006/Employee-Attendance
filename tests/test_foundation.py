import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pymongo.collection import Collection
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

from app import main
from tests.fixtures import ATTENDANCE_LOGS, SAMPLE_8_REGULARIZED, SAMPLE_9_LEGACY

ROOT = Path(__file__).resolve().parent.parent

NEW_EMPLOYEE = {
    "emp_code": "EMP0100",
    "name": "Test Person",
    "email": "test@example.com",
    "department": "Engineering",
    "joined_on": "2026-07-01",
}
ATTENDANCE_KEYS = {
    "emp_code", "date", "status", "punch_in", "punch_out",
    "work_hours", "late_minutes", "overtime_minutes", "half_day", "history",
}
EMPLOYEE_KEYS = {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}


def index_specs():
    specs = {}
    for coll in ("employees", "attendance_logs"):
        for ix in main.db[coll].list_indexes():
            specs[(coll, ix["name"])] = (list(ix["key"].items()), bool(ix.get("unique")))
    return specs


def failing_create_index(calls):
    def fake(self, *args, **kwargs):
        calls.append(self.name)
        raise OperationFailure("forced index build failure")
    return fake


# --------------------------------------------------------------------------- #
# Client and /health
# --------------------------------------------------------------------------- #
def test_client_is_tz_aware_with_short_timeouts():
    assert main.client.codec_options.tz_aware is True
    assert main.client.codec_options.tzinfo == timezone.utc
    assert main.client.options.server_selection_timeout == 3
    assert main.client.options.pool_options.connect_timeout == 3


def test_stored_instants_come_back_tz_aware(api):
    doc = main.db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    assert doc["punch_in"].tzinfo is not None
    assert doc["punch_in"].utcoffset() == timedelta(0)


def test_health_ok(api):
    response = api.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_503_fast_when_mongo_unreachable(api, monkeypatch):
    monkeypatch.setattr(main, "client", main.make_client("mongodb://127.0.0.1:1"))
    started = time.monotonic()
    response = api.get("/health")
    elapsed = time.monotonic() - started
    assert response.status_code == 503
    assert isinstance(response.json()["detail"], str)
    assert elapsed < 5


@pytest.mark.skipif(os.getenv("HRONE_DOCKER_TESTS") != "1", reason="stops the hrone-mongo container; opt in")
def test_health_503_when_container_stopped_then_recovers(api):
    assert api.get("/health").status_code == 200
    subprocess.run(["docker", "stop", "hrone-mongo"], check=True, capture_output=True)
    try:
        started = time.monotonic()
        response = api.get("/health")
        elapsed = time.monotonic() - started
        assert response.status_code == 503
        assert elapsed < 5
    finally:
        subprocess.run(["docker", "start", "hrone-mongo"], check=True, capture_output=True)
    deadline = time.monotonic() + 30
    while api.get("/health").status_code != 200:
        assert time.monotonic() < deadline, "MongoDB did not come back within 30s"
        time.sleep(0.5)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_uvicorn_first_health_200_within_20s():
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        started = time.monotonic()
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() - started < 20, "no 200 from /health within 20s"
            time.sleep(0.2)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
def test_five_indexes_exist_with_expected_keys(api):
    specs = index_specs()
    assert specs[("employees", "emp_code_unique")] == ([("emp_code", 1)], True)
    assert specs[("attendance_logs", "emp_code_date_unique")] == ([("emp_code", 1), ("date", -1)], True)
    assert specs[("attendance_logs", "date_emp_code")] == ([("date", -1), ("emp_code", 1)], False)
    assert specs[("employees", "department_joined_on")] == ([("department", 1), ("joined_on", 1)], False)
    assert specs[("employees", "joined_on")] == ([("joined_on", 1)], False)
    assert len(specs) == 7  # five plus the two _id indexes
    assert all(main.INDEX_STATUS[name] for _, _, name, _ in main.INDEXES)


def test_second_startup_adds_no_indexes(api):
    before = index_specs()
    with TestClient(main.app):
        pass
    main.ensure_indexes()
    assert index_specs() == before


def test_startup_survives_index_failure(monkeypatch):
    from tests.conftest import seed
    seed()
    main.INDEX_STATUS.clear()
    calls = []
    monkeypatch.setattr(Collection, "create_index", failing_create_index(calls))
    with TestClient(main.app) as test_client:
        assert test_client.get("/health").status_code == 200
    assert len(calls) == 5
    assert not any(main.INDEX_STATUS.values())


# --------------------------------------------------------------------------- #
# Critical-index gate
# --------------------------------------------------------------------------- #
def test_gate_refuses_employee_create_when_index_dropped_and_retry_fails(api, monkeypatch):
    first = api.post("/employees", json=NEW_EMPLOYEE)
    assert first.status_code == 201
    assert main.INDEX_STATUS["emp_code_unique"] is True

    main.db.employees.drop_index("emp_code_unique")
    count_before = main.db.employees.count_documents({})
    calls = []
    monkeypatch.setattr(Collection, "create_index", failing_create_index(calls))

    response = api.post("/employees", json={**NEW_EMPLOYEE, "emp_code": "EMP0101", "email": "other@example.com"})

    assert response.status_code == 503
    assert calls == ["employees"]  # exactly one retry
    assert main.db.employees.count_documents({}) == count_before
    assert main.INDEX_STATUS["emp_code_unique"] is False
    assert api.get("/health").status_code == 200


def test_gate_refuses_punch_in_when_index_dropped_and_retry_fails(api, monkeypatch):
    first = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1784520000000})
    assert first.status_code == 201
    assert main.INDEX_STATUS["emp_code_date_unique"] is True

    main.db.attendance_logs.drop_index("emp_code_date_unique")
    count_before = main.db.attendance_logs.count_documents({})
    calls = []
    monkeypatch.setattr(Collection, "create_index", failing_create_index(calls))

    response = api.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": 1784520000000})

    assert response.status_code == 503
    assert calls == ["attendance_logs"]
    assert main.db.attendance_logs.count_documents({}) == count_before
    assert main.INDEX_STATUS["emp_code_date_unique"] is False
    assert api.get("/health").status_code == 200


def test_gate_recreates_missing_index_and_allows_write(api):
    main.db.employees.drop_index("emp_code_unique")
    response = api.post("/employees", json=NEW_EMPLOYEE)
    assert response.status_code == 201
    assert index_specs()[("employees", "emp_code_unique")] == ([("emp_code", 1)], True)


def test_gate_rejects_non_unique_index_with_same_keys(api, monkeypatch):
    main.db.employees.drop_index("emp_code_unique")
    main.db.employees.create_index([("emp_code", 1)], name="emp_code_plain")
    calls = []
    monkeypatch.setattr(Collection, "create_index", failing_create_index(calls))
    count_before = main.db.employees.count_documents({})
    response = api.post("/employees", json=NEW_EMPLOYEE)
    assert response.status_code == 503
    assert main.db.employees.count_documents({}) == count_before


def test_gate_503_when_index_listing_fails(api, monkeypatch):
    count_before = main.db.employees.count_documents({})

    def broken(self, *args, **kwargs):
        raise ServerSelectionTimeoutError("forced listing failure")

    monkeypatch.setattr(Collection, "list_indexes", broken)
    response = api.post("/employees", json=NEW_EMPLOYEE)
    monkeypatch.undo()
    assert response.status_code == 503
    assert main.db.employees.count_documents({}) == count_before


# --------------------------------------------------------------------------- #
# Conversions
# --------------------------------------------------------------------------- #
def test_to_ms_matches_contract_example():
    # openapi.yaml HistoryEntry example values: 2026-07-06 04:35Z and 03:58Z
    assert main.to_ms(datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc)) == 1783312500000
    assert main.to_ms(datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc)) == 1783310280000


def test_to_ms_edge_cases():
    ist = timezone(timedelta(hours=5, minutes=30))
    instant = datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc)
    assert main.to_ms(None) is None
    assert main.to_ms(instant.astimezone(ist)) == 1783312500000
    assert main.to_ms(datetime(2026, 7, 6, 4, 35)) == 1783312500000  # naive is UTC
    assert main.to_ms(datetime(2026, 7, 6, 4, 35, 0, 123456, tzinfo=timezone.utc)) == 1783312500123
    assert isinstance(main.to_ms(instant), int)


def test_from_ms_round_trip():
    value = main.from_ms(1783312500000)
    assert value == datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc)
    assert value.utcoffset() == timedelta(0)
    assert main.to_ms(main.from_ms(1783312500123)) == 1783312500123


@pytest.mark.parametrize(
    "value, places, expected",
    [
        (8.125, 2, 8.13),
        (Decimal(29250) / Decimal(3600), 2, 8.13),  # 8h07m30s
        (2.675, 2, 2.68),
        (0.375, 2, 0.38),
        (4.495, 2, 4.5),
        (2.5, 0, 3.0),
        (-2.5, 0, -3.0),
        (26.19045, 4, 26.1905),
        (Decimal("5.5") / Decimal(21) * 100, 4, 26.1905),
        (3, 2, 3.0),
        (None, 2, None),
    ],
)
def test_round_half_up(value, places, expected):
    assert main.round_half_up(value, places) == expected


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def test_create_employee_returns_created_at_as_epoch_ms(api):
    response = api.post("/employees", json=NEW_EMPLOYEE)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == EMPLOYEE_KEYS
    assert isinstance(body["created_at"], int)
    assert 1.7e12 <= body["created_at"] < 1.9e12


def test_employee_out_shape():
    doc = {"_id": "x", "id": "y", "emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "D",
           "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05",
           "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc)}
    out = main.employee_out(doc)
    assert set(out) == EMPLOYEE_KEYS
    assert out["created_at"] == 1767587400000


def test_attendance_out_on_every_sample_read_from_mongo(api):
    docs = list(main.db.attendance_logs.find())
    assert len(docs) == len(ATTENDANCE_LOGS)
    for doc in docs:
        out = main.attendance_out(doc)
        assert set(out) == ATTENDANCE_KEYS
        for field in ("punch_in", "punch_out"):
            assert out[field] is None or isinstance(out[field], int)
        assert out["work_hours"] == doc.get("work_hours")


def test_attendance_out_converts_nested_history_instants(api):
    doc = main.db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-07"})
    out = main.attendance_out(doc)
    assert out["punch_in"] == 1783396680000
    assert out["history"] == [
        {
            "at": 1783486800000,
            "by": "hr.admin",
            "reason": "biometric glitch",
            "changes": {
                "punch_in": {"from": 1783398900000, "to": 1783396680000},
                "late_minutes": {"from": 35, "to": 0},
                "work_hours": {"from": 8.42, "to": 9.03},
            },
        }
    ]
    assert SAMPLE_8_REGULARIZED["history"][0]["at"] == main.from_ms(1783486800000)


def test_attendance_out_legacy_defaults(api):
    doc = main.db.attendance_logs.find_one({"emp_code": "EMP0002", "date": "2026-07-14"})
    assert "half_day" not in doc and "history" not in doc
    out = main.attendance_out(doc)
    assert out["half_day"] is False
    assert out["history"] == []
    assert out["punch_in"] == main.to_ms(SAMPLE_9_LEGACY["punch_in"])


def test_attendance_out_missing_minutes_default_to_zero():
    out = main.attendance_out({"_id": "x", "id": "y", "emp_code": "EMP0004", "date": "2026-07-08", "status": "ABSENT"})
    assert set(out) == ATTENDANCE_KEYS
    assert out["late_minutes"] == 0 and out["overtime_minutes"] == 0
    assert out["punch_in"] is None and out["punch_out"] is None and out["work_hours"] is None
