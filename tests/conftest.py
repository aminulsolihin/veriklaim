"""Test setup: a throwaway database and known passwords, set before the app is imported.

Set TEST_DATABASE_URL to run the same tests against Postgres."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="veriklaim-test-")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite:///{}/test.db".format(_tmp)
os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["VERIFIKATOR_PASSWORD"] = "verif-pass-1"
os.environ["SUPERVISOR_PASSWORD"] = "super-pass-1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

HEADERS = {"X-Requested-With": "veriklaim"}


@pytest.fixture(scope="session")
def server():
    Base.metadata.drop_all(engine)  # a clean start, also when pointed at a reused Postgres database
    with TestClient(app) as c:  # runs start-up: tables, accounts, reference prices, sample batch
        yield c


def _client(server, username, password):
    c = TestClient(server.app)
    r = c.post("/api/login", json={"username": username, "password": password}, headers=HEADERS)
    assert r.status_code == 200, r.text
    c.headers.update(HEADERS)
    return c


@pytest.fixture()
def verifikator(server):
    return _client(server, "verifikator", "verif-pass-1")


@pytest.fixture()
def supervisor(server):
    return _client(server, "supervisor", "super-pass-1")


@pytest.fixture(scope="session")
def sample_text():
    from app.config import settings
    return (settings.data_dir / "sample-eklaim.txt").read_text(encoding="utf-8")
