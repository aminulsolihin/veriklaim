"""Judge mode: one-click guest sessions with a private copy of the synthetic sample."""
from datetime import timedelta

from fastapi.testclient import TestClient

from app import main, services
from app.db import Batch, SessionLocal, User, utcnow

from .conftest import HEADERS


def _guest(server):
    c = TestClient(server.app)
    c.headers.update(HEADERS)
    r = c.post("/api/demo-login")
    assert r.status_code == 200, r.text
    return c, r.json()


def test_guest_gets_a_private_scored_copy(server, verifikator):
    c, me = _guest(server)
    assert me["role"] == "juri" and me["guest"] and me["expiresAt"]
    batches = c.get("/api/batches").json()
    assert len(batches) == 1 and batches[0]["own"] and batches[0]["labelled"]
    claims = c.get("/api/batches/{}".format(batches[0]["id"])).json()["claims"]
    team = verifikator.get("/api/batches").json()
    team_claims = verifikator.get("/api/batches/{}".format(team[-1]["id"])).json()["claims"]
    # the copy carries the same scores and ranks, so the deck's numbers hold for every judge
    assert [(x["sep"], x["rank"], round(x["score"], 9)) for x in claims] == \
           [(x["sep"], x["rank"], round(x["score"], 9)) for x in team_claims]
    assert batches[0]["flagged"] == 16 and batches[0]["reviewed"] == 0
    # the hidden template and other people's batches are not listed to the team
    assert all(not b["own"] for b in team)


def test_guests_cannot_see_each_other_or_the_team(server, verifikator):
    a, _ = _guest(server)
    b, _ = _guest(server)
    a_batch = a.get("/api/batches").json()[0]
    a_claim = a.get("/api/batches/{}".format(a_batch["id"])).json()["claims"][0]
    team_batch = verifikator.get("/api/batches").json()[0]
    assert b.get("/api/batches/{}".format(a_batch["id"])).status_code == 404
    assert b.get("/api/claims/{}".format(a_claim["id"])).status_code == 404
    assert b.put("/api/claims/{}/review".format(a_claim["id"]), json={"status": "cleared"}).status_code == 404
    assert verifikator.get("/api/batches/{}".format(a_batch["id"])).status_code == 404
    assert a.get("/api/batches/{}".format(team_batch["id"])).status_code == 404
    assert b.delete("/api/batches/{}".format(a_batch["id"])).status_code == 404


def test_guest_permissions(server):
    c, me = _guest(server)
    batch = c.get("/api/batches").json()[0]
    claim = c.get("/api/batches/{}".format(batch["id"])).json()["claims"][0]
    assert c.put("/api/claims/{}/review".format(claim["id"]),
                 json={"status": "docs_requested", "note": "uji juri"}).status_code == 200
    r = c.patch("/api/batches/{}".format(batch["id"]), json={"thresholdPct": 120, "topK": 14})
    assert r.status_code == 200 and r.json()["topK"] == 14
    # reference prices are shared, so a guest can read them but not change them
    assert c.get("/api/reference-prices").status_code == 200
    assert c.put("/api/reference-prices/RR04", json={"kind": "SR", "description": "x", "price": 1}).status_code == 403
    log = c.get("/api/audit").json()
    assert log and {a["username"] for a in log} == {me["username"]}
    assert {"demo_login", "review_set", "batch_settings"} <= {a["action"] for a in log}
    assert c.get("/api/usage").status_code == 403
    assert c.delete("/api/batches/{}".format(batch["id"])).status_code == 200
    assert c.get("/api/batches").json() == []


def test_guest_upload_limit(server, sample_text, monkeypatch):
    c, _ = _guest(server)
    monkeypatch.setattr(main.settings, "guest_max_batches", 2)
    r = c.post("/api/batches", files={"file": ("rs.txt", sample_text.encode(), "text/plain")})
    assert r.status_code == 200 and r.json()["own"]
    r = c.post("/api/batches", files={"file": ("rs.txt", sample_text.encode(), "text/plain")})
    assert r.status_code == 400 and "Mode juri" in r.json()["detail"]


def test_expired_guests_are_signed_out_and_purged(server):
    c, me = _guest(server)
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=me["username"]).one()
        user.created_at = utcnow() - timedelta(hours=25)
        db.commit()
    assert c.get("/api/me").status_code == 401
    with SessionLocal() as db:
        assert services.purge_guests(db) >= 1
        assert db.query(User).filter_by(username=me["username"]).count() == 0
        assert db.query(Batch).filter_by(owner=me["username"]).count() == 0
        assert db.query(Batch).filter_by(owner="system:template").count() == 1


def test_guest_sessions_are_rate_limited_per_network(server, monkeypatch):
    monkeypatch.setattr(main, "guest_limiter", main.LoginLimiter(limit=2, window=3600))
    c = TestClient(server.app)
    c.headers.update(HEADERS)
    assert c.post("/api/demo-login").status_code == 200
    assert c.post("/api/demo-login").status_code == 200
    assert c.post("/api/demo-login").status_code == 429


def test_judge_mode_can_be_switched_off(server, monkeypatch):
    monkeypatch.setattr(main.settings, "judge_mode", False)
    c = TestClient(server.app)
    assert c.post("/api/demo-login", headers=HEADERS).status_code == 404
    assert c.get("/api/config").json()["judgeMode"] is False


def test_demo_login_needs_the_csrf_header(server):
    assert TestClient(server.app).post("/api/demo-login").status_code == 403


def test_feedback_and_usage(server, supervisor):
    c, me = _guest(server)
    body = {"peran": "juri", "institusi": "", "useful": 4, "wouldUse": "mungkin", "comment": "Alasannya jelas."}
    assert c.post("/api/feedback", json=body).status_code == 200
    assert c.post("/api/feedback", json=dict(body, useful=9)).status_code == 422
    assert c.get("/api/feedback").status_code == 403
    rows = supervisor.get("/api/feedback").json()
    assert any(f["username"] == me["username"] and f["comment"] == "Alasannya jelas." for f in rows)
    assert "Alasannya jelas." in supervisor.get("/api/feedback.csv").text
    u = supervisor.get("/api/usage").json()
    assert u["guestSessions"] >= 1 and u["feedback"] >= 1 and 1 <= u["avgUseful"] <= 5


def test_healthz_does_not_touch_the_database(server):
    from sqlalchemy import event
    from app.db import engine
    seen = []
    listener = lambda *a, **k: seen.append(1)  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        assert server.get("/healthz").json() == {"ok": True}
        assert seen == []
        assert server.get("/readyz").json() == {"ok": True}
        assert seen  # the readiness check does query
    finally:
        event.remove(engine, "before_cursor_execute", listener)
