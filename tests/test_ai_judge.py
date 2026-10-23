"""The Claude second opinion, with the API client replaced by a fake. No network, no key needed."""
import json
from types import SimpleNamespace

import pytest

from app import ai_judge
from app.config import settings


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        out = {"recommendation": "docs_requested", "confidence": "medium", "summary": "Perlu faktur.",
               "reasons": ["Tagihan RS di bawah harga acuan."], "documents": ["Faktur implan"]}
        return SimpleNamespace(stop_reason="end_turn", model=kw["model"], _request_id="req_test",
                               content=[SimpleNamespace(type="text", text=json.dumps(out))])


@pytest.fixture()
def fake(monkeypatch):
    msgs = FakeMessages()
    monkeypatch.setattr(settings, "ai_judge", True)
    monkeypatch.setattr(ai_judge, "_client", SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
    return msgs


def _first_claim(c):
    b = next(x for x in c.get("/api/batches").json() if x["rowsScreened"] == 80)
    return c.get("/api/batches/{}".format(b["id"])).json()["claims"][0]


def test_off_without_key(verifikator, monkeypatch):
    monkeypatch.setattr(settings, "ai_judge", False)
    r = verifikator.post("/api/claims/{}/ai-opinion".format(_first_claim(verifikator)["id"]))
    assert r.status_code == 503


def test_opinion_is_advice_and_sends_no_identifiers(verifikator, supervisor, fake):
    claim = _first_claim(verifikator)
    detail = verifikator.get("/api/claims/{}".format(claim["id"])).json()
    r = verifikator.post("/api/claims/{}/ai-opinion".format(claim["id"]))
    assert r.status_code == 200, r.text
    assert r.json()["recommendation"] == "docs_requested"
    sent = fake.calls[-1]["messages"][0]["content"]
    assert detail["sep"] not in sent and detail["token"] not in sent
    assert verifikator.get("/api/claims/{}".format(claim["id"])).json()["review"] is None
    actions = [a["action"] for a in supervisor.get("/api/audit").json()]
    assert "ai_opinion" in actions
