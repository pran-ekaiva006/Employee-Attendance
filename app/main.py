"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import ConnectionFailure, PyMongoError

load_dotenv()

logger = logging.getLogger("attendance")

def make_client(uri: str) -> MongoClient:
    return MongoClient(
        uri,
        tz_aware=True,
        tzinfo=timezone.utc,
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
    )


client = make_client(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
# (collection, keys, name, unique)
INDEXES = [
    ("employees", [("emp_code", ASCENDING)], "emp_code_unique", True),
    ("attendance_logs", [("emp_code", ASCENDING), ("date", DESCENDING)], "emp_code_date_unique", True),
    ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], "date_emp_code", False),
    ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], "department_joined_on", False),
    ("employees", [("joined_on", ASCENDING)], "joined_on", False),
]
CRITICAL_INDEXES = {coll: (keys, name) for coll, keys, name, unique in INDEXES if unique}
INDEX_STATUS: dict[str, bool] = {}


def ensure_indexes() -> None:
    """Create every index idempotently. Failures are logged, never raised, so startup continues."""
    for coll, keys, name, unique in INDEXES:
        try:
            db[coll].create_index(keys, name=name, unique=unique)
            INDEX_STATUS[name] = True
        except ConnectionFailure as exc:
            logger.error("MongoDB unreachable while creating indexes, skipping the rest: %s", exc)
            for _, _, pending, _ in INDEXES:
                INDEX_STATUS.setdefault(pending, False)
            return
        except PyMongoError as exc:
            logger.error("Could not create index %s on %s: %s", name, coll, exc)
            INDEX_STATUS[name] = False


def unique_index_present(collection: str, keys: list) -> bool:
    for index in db[collection].list_indexes():
        if index.get("unique") and [(k, int(v)) for k, v in index["key"].items()] == keys:
            return True
    return False


def require_critical_index(collection: str) -> None:
    """Refuse a write whose duplicate protection depends on a unique index that is not actually there.

    INDEX_STATUS can be stale (an index can be dropped after startup), so the live index list is checked every time.
    """
    keys, name = CRITICAL_INDEXES[collection]
    try:
        if unique_index_present(collection, keys):
            INDEX_STATUS[name] = True
            return
    except PyMongoError as exc:
        INDEX_STATUS[name] = False
        logger.error("Could not list indexes on %s: %s", collection, exc)
        raise HTTPException(503, "required index could not be verified")

    logger.warning("Unique index %s on %s is missing, retrying creation once", name, collection)
    try:
        db[collection].create_index(keys, name=name, unique=True)
        present = unique_index_present(collection, keys)
    except PyMongoError as exc:
        logger.error("Retry of index %s on %s failed: %s", name, collection, exc)
        present = False
    INDEX_STATUS[name] = present
    if not present:
        logger.error("Unique index %s on %s is unavailable, refusing the write", name, collection)
        raise HTTPException(503, "required index is unavailable")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Conversions and serializers
# --------------------------------------------------------------------------- #
def to_ms(value: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> integer epoch milliseconds. Naive values are UTC, as PyMongo stores them."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (value - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def round_half_up(value, places: int = 2) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, float):
        value = Decimal(str(value))
    return float(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")


def employee_out(doc: dict) -> dict:
    out = {field: doc.get(field) for field in EMPLOYEE_FIELDS}
    out["created_at"] = to_ms(doc.get("created_at"))
    return out


def _change_value(value):
    return to_ms(value) if isinstance(value, datetime) else value


def history_out(entry: dict) -> dict:
    return {
        "at": to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {"from": _change_value(change.get("from")), "to": _change_value(change.get("to"))}
            for field, change in (entry.get("changes") or {}).items()
        },
    }


def attendance_out(doc: dict) -> dict:
    """The ten AttendanceRecord keys. Missing late/overtime mean 0, half_day false, history empty (DATA_MODEL.md)."""
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(entry) for entry in doc.get("history") or []],
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        logger.warning("Health check ping failed: %s", exc)
        raise HTTPException(503, "MongoDB is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    require_critical_index("employees")
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = page * page_size
    total = db.employees.count_documents({})
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    require_critical_index("attendance_logs")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
