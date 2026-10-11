"""The API: sign-in, roles, the review workflow, uploads, reference prices, the audit log."""
import io

from fastapi.testclient import TestClient

from tests.conftest import HEADERS


def _sample_batch(client):
    batches = client.get("/api/batches").json()
    return next(b for b in batches if b["labelled"])


def test_health_and_config_are_public(server):
    assert server.get("/healthz").json() == {"ok": True}
    cfg = server.get("/api/config").json()
    assert cfg["minClaims"] == 20 and len(cfg["features"]) == 4


def test_api_needs_a_session(server):
    from fastapi.testclient import TestClient
    anon = TestClient(server.app)
    for path in ("/api/me", "/api/batches", "/api/batches/1", "/api/claims/1", "/api/reference-prices", "/api/audit"):
        assert anon.get(path).status_code == 401, path


def test_wrong_password_is_rejected(server):
    from fastapi.testclient import TestClient
    anon = TestClient(server.app)
    r = anon.post("/api/login", json={"username": "supervisor", "password": "nope"}, headers=HEADERS)
    assert r.status_code == 401
    r = anon.post("/api/login", json={"username": "nobody", "password": "nope"}, headers=HEADERS)
    assert r.status_code == 401


def test_changes_need_the_request_header(server):
    from fastapi.testclient import TestClient
    c = TestClient(server.app)
    assert c.post("/api/login", json={"username": "supervisor", "password": "super-pass-1"}).status_code == 403
    c.post("/api/login", json={"username": "supervisor", "password": "super-pass-1"}, headers=HEADERS)
    assert c.put("/api/claims/1/review", json={"status": "cleared", "note": ""}).status_code == 403


def test_seeded_sample_batch(verifikator):
    b = _sample_batch(verifikator)
    assert b["rowsScreened"] == 80 and b["thresholdPct"] == 100 and b["topK"] == 12
    assert b["flagged"] in (15, 16) and b["reviewed"] == 0
    d = verifikator.get("/api/batches/{}".format(b["id"])).json()
    claims = d["claims"]
    assert [c["rank"] for c in claims] == list(range(1, 81))
    assert sum(1 for c in claims if c["cover"] < 1) == 7
    assert sum(1 for c in claims if c["label"]) == 14
    assert "nama" not in " ".join(claims[0].keys()).lower()


def test_claim_detail_explains_on_demand(verifikator):
    b = _sample_batch(verifikator)
    claims = verifikator.get("/api/batches/{}".format(b["id"])).json()["claims"]
    late = claims[-1]                       # rank 80: not explained at upload
    assert late["phi"] is None
    d = verifikator.get("/api/claims/{}".format(late["id"])).json()
    assert len(d["phi"]) == 4
    assert abs(sum(d["phi"]) - (d["score"] - b["baseScore"])) < 1e-9
    assert d["token"].startswith("P-")
    again = verifikator.get("/api/batches/{}".format(b["id"])).json()["claims"][-1]
    assert again["phi"] == d["phi"]         # stored after the first request


def test_review_workflow_and_audit(verifikator, supervisor):
    b = _sample_batch(verifikator)
    claim = verifikator.get("/api/batches/{}".format(b["id"])).json()["claims"][0]
    url = "/api/claims/{}/review".format(claim["id"])

    r = verifikator.put(url, json={"status": "confirmed", "note": "Implant record missing."})
    assert r.status_code == 200
    assert r.json()["review"]["status"] == "confirmed" and r.json()["review"]["by"] == "verifikator"
    assert _sample_batch(verifikator)["reviewed"] == 1
    assert verifikator.put(url, json={"status": "maybe", "note": ""}).status_code == 422

    assert verifikator.get("/api/audit").status_code == 403
    log = supervisor.get("/api/audit").json()
    entry = next(a for a in log if a["action"] == "review_set")
    assert entry["sep"] == claim["sep"] and entry["username"] == "verifikator"

    r = supervisor.put(url, json={"status": None, "note": ""})
    assert r.json()["review"] is None
    assert _sample_batch(verifikator)["reviewed"] == 0


def test_hospital_response_is_stored_exported_and_audited(verifikator, supervisor):
    b = _sample_batch(verifikator)
    claim = verifikator.get("/api/batches/{}".format(b["id"])).json()["claims"][1]
    url = "/api/claims/{}/review".format(claim["id"])
    r = verifikator.put(url, json={"status": "docs_requested", "note": "Minta faktur implan.",
                                   "rsResponse": "RS mengirim faktur no. 123."})
    assert r.status_code == 200 and r.json()["review"]["rsResponse"] == "RS mengirim faktur no. 123."
    csv_text = verifikator.get("/api/batches/{}/export.csv".format(b["id"])).text
    assert "RS_RESPONSE" in csv_text and "RS mengirim faktur no. 123." in csv_text
    entry = next(a for a in supervisor.get("/api/audit").json() if a["action"] == "review_set")
    assert "faktur no. 123" in entry["detail"]
    r = verifikator.put(url, json={"status": None, "note": ""})
    assert r.json()["review"] is None
    assert verifikator.get("/api/claims/{}".format(claim["id"])).json()["review"] is None


def test_older_database_gains_the_new_column(tmp_path):
    from sqlalchemy import create_engine, inspect, text
    from app import db as dbm
    old = create_engine("sqlite:///{}/old.db".format(tmp_path))
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE claims (id INTEGER PRIMARY KEY, review_note TEXT)"))
    saved, dbm.engine = dbm.engine, old
    try:
        dbm.add_missing_columns()
        dbm.add_missing_columns()  # running twice is harmless
    finally:
        dbm.engine = saved
    assert "rs_response" in {c["name"] for c in inspect(old).get_columns("claims")}


def test_only_a_supervisor_changes_thresholds(verifikator, supervisor):
    b = _sample_batch(verifikator)
    url = "/api/batches/{}".format(b["id"])
    assert verifikator.patch(url, json={"thresholdPct": 150, "topK": 20}).status_code == 403
    r = supervisor.patch(url, json={"thresholdPct": 150, "topK": 20})
    assert r.status_code == 200 and r.json()["thresholdPct"] == 150 and r.json()["topK"] == 20
    assert supervisor.patch(url, json={"thresholdPct": 500, "topK": 20}).status_code == 422
    supervisor.patch(url, json={"thresholdPct": 100, "topK": 12})
    assert _sample_batch(verifikator)["topK"] == 12


def test_upload_scores_and_stores_a_new_batch(verifikator, supervisor, sample_text):
    r = verifikator.post("/api/batches", files={"file": ("rs-april.txt", sample_text.encode(), "text/plain")},
                         data={"name": "RS April"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["name"] == "RS April" and b["rowsScreened"] == 80 and not b["labelled"]
    assert b["uploadedBy"] == "verifikator"
    claims = verifikator.get("/api/batches/{}".format(b["id"])).json()["claims"]
    assert all(c["label"] is None for c in claims)

    assert verifikator.delete("/api/batches/{}".format(b["id"])).status_code == 403
    assert supervisor.delete("/api/batches/{}".format(b["id"])).status_code == 200
    assert verifikator.get("/api/batches/{}".format(b["id"])).status_code == 404
    assert verifikator.get("/api/claims/{}".format(claims[0]["id"])).status_code == 404


def test_upload_rejects_bad_files_with_a_message(verifikator, sample_text):
    r = verifikator.post("/api/batches", files={"file": ("x.txt", b"not an e-klaim file\nfoo", "text/plain")})
    assert r.status_code == 400 and "tab-separated" in r.json()["detail"]
    few = "\n".join(sample_text.split("\n")[:6])
    r = verifikator.post("/api/batches", files={"file": ("few.txt", few.encode(), "text/plain")})
    assert r.status_code == 400 and "minimal 20" in r.json()["detail"]
    n_before = len(verifikator.get("/api/batches").json())
    assert n_before == len(verifikator.get("/api/batches").json())


def test_reference_prices_are_supervisor_only_and_drive_screening(verifikator, supervisor, sample_text):
    refs = verifikator.get("/api/reference-prices").json()
    assert {r["code"] for r in refs} == {"RR04", "DD01"}
    body = {"kind": "SR", "description": "Test implant", "price": 1000, "source": "test"}
    assert verifikator.put("/api/reference-prices/ZZ99", json=body).status_code == 403
    assert supervisor.put("/api/reference-prices/bad-code", json=body).status_code == 400
    assert supervisor.put("/api/reference-prices/zz99", json=body).json()["code"] == "ZZ99"

    # without the streptokinase price, those claims are skipped and counted
    assert supervisor.delete("/api/reference-prices/DD01").status_code == 200
    r = supervisor.post("/api/batches", files={"file": ("s.txt", sample_text.encode(), "text/plain")})
    b = r.json()
    assert b["noRef"] > 0 and b["rowsScreened"] + b["noRef"] == 80
    supervisor.delete("/api/batches/{}".format(b["id"]))
    supervisor.delete("/api/reference-prices/ZZ99")
    supervisor.put("/api/reference-prices/DD01", json={"kind": "SD", "description": "Streptokinase",
                                                       "price": 4_000_000, "source": "Placeholder"})


def test_csv_export(verifikator):
    b = _sample_batch(verifikator)
    r = verifikator.get("/api/batches/{}/export.csv".format(b["id"]))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.lstrip("﻿").strip().split("\r\n")
    assert lines[0].startswith('"SEP","KODE_RS"') and len(lines) - 1 == b["flagged"]
    r = verifikator.get("/api/batches/{}/export.csv?view=all".format(b["id"]))
    assert len(r.text.strip().split("\r\n")) - 1 == 80


def test_csv_cells_cannot_run_as_formulas(verifikator, supervisor, sample_text):
    lines = sample_text.strip().split("\n")
    cols = lines[0].split("\t")
    row = lines[1].split("\t")
    row[cols.index("SEP")] = "=HYPERLINK(\"http://x\")"
    r = supervisor.post("/api/batches", files={"file": ("f.txt", "\n".join([lines[0], "\t".join(row)] + lines[2:]).encode(), "text/plain")})
    b = r.json()
    text = supervisor.get("/api/batches/{}/export.csv?view=all".format(b["id"])).text
    assert "\"'=HYPERLINK" in text and "\"=HYPERLINK" not in text
    supervisor.delete("/api/batches/{}".format(b["id"]))


def test_pages_and_security_headers(server):
    r = server.get("/")
    assert r.status_code == 200 and "VeriKlaim" in r.text
    assert "connect-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert server.get("/assets/js/app.js").status_code == 200
    assert server.get("/assets/sample/veriklaim-sample-eklaim.txt").status_code == 200
    assert server.get("/api/nope").status_code == 404


def test_robustness_requires_login_and_matches_the_published_figures(server, verifikator):
    anon = TestClient(server.app)
    assert anon.get("/api/robustness", headers=HEADERS).status_code == 401
    d = verifikator.get("/api/robustness").json()
    assert d["n"] == 2000 and d["seeds"] == 20
    assert [lv["id"] for lv in d["levels"]] == ["L1", "L2", "L3", "L4"]
    i5 = d["fractions"].index(0.05)
    # the numbers shown on slide 13 (recall at 5% workload) and the scale runs
    assert [round(lv["recall"][i5] * 100, 1) for lv in d["levels"]] == [100.0, 97.5, 82.2, 22.8]
    assert [r["rows"] for r in d["benchmark"]["runs"]] == [1530000, 1000000]
    for lv in d["levels"]:
        n = len(d["fractions"])
        assert len(lv["recall"]) == len(lv["precision"]) == len(lv["legitInQueue"]) == n
        assert all(0 <= v <= 1 for v in lv["recall"])
        assert lv["recall"] == sorted(lv["recall"])  # more workload never lowers recall


def test_robustness_is_open_to_judge_sessions(server):
    from tests.test_judge import _guest
    c, _ = _guest(server)
    assert c.get("/api/robustness").status_code == 200
