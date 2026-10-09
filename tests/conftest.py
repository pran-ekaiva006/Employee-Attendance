import copy
import os

import pytest

# Tests own a separate database; it is dropped and reseeded for every test.
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = os.getenv("TEST_MONGO_DB", "attendance_test")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from tests.fixtures import ATTENDANCE_LOGS, EMPLOYEES  # noqa: E402


def seed():
    main.client.drop_database(main.db.name)
    main.db.employees.insert_many(copy.deepcopy(EMPLOYEES))
    main.db.attendance_logs.insert_many(copy.deepcopy(ATTENDANCE_LOGS))


@pytest.fixture
def api():
    seed()
    main.INDEX_STATUS.clear()
    with TestClient(main.app) as test_client:
        yield test_client
