"""Upcoding check: reader, indicators, hospital pattern, API and judge-mode isolation.

The sample is synthetic and its upcoding is planted by rules the check also uses, so these tests
show the code does what it says; they do not measure accuracy on real claims."""
import json
from datetime import timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import coding, services
from app.config import settings
from app.db import CodingBatch, CodingClaim, SessionLocal, User, utcnow
from app.eklaim import EklaimError

from .conftest import HEADERS


@pytest.fixture(scope="session")
def text():
    return (settings.data_dir / "sample-coding.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def key():
    return json.loads((settings.data_dir / "sample-coding-labels.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def screened(text):
    rows = coding.parse_coding(text)["rows"]
    model, info = coding.screen(rows)
    return rows, model, info


def _guest(server):
    c = TestClient(server.app)
    c.headers.update(HEADERS)
    assert c.post("/api/demo-login").status_code == 200
    return c


def _team_batch(client):
    return client.get("/api/coding/batches").json()[0]


# ---------- reader ----------

def test_reader_keeps_only_what_the_check_needs(text):
    parsed = coding.parse_coding(text)
    assert parsed["total"] == 1087 and len(parsed["rows"]) == 1087 and parsed["bad"] == 0
    r = parsed["rows"][0]
    assert r["grp"] == r["inacbg"].rsplit("-", 1)[0] and r["sev"] in (1, 2, 3)
    blob = json.dumps(parsed["rows"])
    assert "PASIEN" not in blob and "9994600" not in blob          # no name, no card number
    assert not {"nama", "mrn", "birth", "nokartu", "card_token"} & {k.lower() for k in r}


def test_reader_skips_unreadable_rows_and_rejects_bad_files(text):
    lines = text.splitlines()
    parsed = coding.parse_coding("\n".join(lines[:5] + ["x\ty"]))
    assert parsed["bad"] == 1
    with pytest.raises(EklaimError):
        coding.parse_coding("satu baris saja")
    with pytest.raises(EklaimError):
        coding.parse_coding("A B C D E F G H I J K\n1 2 3")          # not tab-separated
    head = lines[0].replace("DIAGLIST", "XDIAG")
    with pytest.raises(EklaimError, match="DIAGLIST"):
        coding.parse_coding("\n".join([head] + lines[1:3]))


def test_absurd_numbers_are_capped_so_they_cannot_break_storage(text):
    lines = text.splitlines()
    head = lines[0].split("\t")
    cells = lines[1].split("\t")
    cells[head.index("TARIF_INACBG")] = "9" * 40
    cells[head.index("LOS")] = "9" * 40
    row = coding.parse_coding("\n".join([lines[0], "\t".join(cells)]))["rows"][0]
    assert row["tarif"] == 10 ** 12 and row["los"] == 100_000


def test_a_file_that_is_too_small_or_has_nothing_to_compare_is_refused(server, verifikator, text):
    lines = text.splitlines()
    small = "\n".join(lines[:30])
    r = verifikator.post("/api/coding/batches", files={"file": ("k.txt", small.encode(), "text/plain")})
    assert r.status_code == 400 and "minimal" in r.json()["detail"]
    head = lines[0].split("\t")
    ix = head.index("INACBG")
    alone = [lines[0]]
    for i, ln in enumerate(lines[1:61]):                      # every claim in a case group of its own
        cells = ln.split("\t")
        cells[ix] = "{}-{}-{:02d}-I".format(chr(65 + i % 26), i // 26, i)
        alone.append("\t".join(cells))
    r = verifikator.post("/api/coding/batches", files={"file": ("k.txt", "\n".join(alone).encode(), "text/plain")})
    assert r.status_code == 400 and "dibandingkan" in r.json()["detail"]


def test_a_single_hospital_gets_claim_checks_but_no_hospital_comparison(supervisor, verifikator, text):
    lines = text.splitlines()
    one = "\n".join([lines[0]] + [ln for ln in lines[1:] if ln.startswith("9991001\t")])
    r = verifikator.post("/api/coding/batches", files={"file": ("k.txt", one.encode(), "text/plain")})
    assert r.status_code == 200
    b = r.json()
    assert b["hospitalsN"] == 1 and b["smallBase"] is True
    d = verifikator.get("/api/coding/batches/{}".format(b["id"])).json()
    assert d["hospitals"] == []
    supervisor.delete("/api/coding/batches/{}".format(b["id"]))


# ---------- indicators ----------

def test_features_are_one_sided_and_level_one_is_never_flagged(screened):
    rows, _, _ = screened
    assert all(min(r["f"]) >= 0 for r in rows)
    low = [r for r in rows if r["sev"] == 1]
    assert low and all(not any(r["f"]) and not r["rule"] and not r["live"] for r in low)
    died = [r for r in rows if r["died"]]
    assert died and all(not any(r["f"]) for r in died)           # a death with a short stay is not a signal


def test_rule_needs_padded_diagnoses_and_no_support(screened):
    rows, _, _ = screened
    flagged = [r for r in rows if r["rule"]]
    assert 20 < len(flagged) < 80
    for r in flagged:
        assert r["sev"] >= 2 and not r["icu"] and not r["vent"] and not r["died"]
        assert r["los"] <= r["base_los"] and r["n_sec"] - r["care_dx"] >= coding.RULE_EXCESS


def test_shapley_values_add_up_to_score_minus_average(screened):
    rows, model, _ = screened
    explained = [r for r in rows if r["phi"] is not None]
    assert len(explained) >= 20 and all(r["live"] for r in explained)
    for r in explained[:10]:
        assert sum(r["phi"]) == pytest.approx(r["score"] - model.base, abs=1e-6)


def test_screening_is_repeatable_and_ranks_live_claims_first(text, screened):
    rows, _, _ = screened
    again = coding.parse_coding(text)["rows"]
    coding.screen(again)
    assert [r["rank"] for r in rows] == [r["rank"] for r in again]
    ranks = sorted(r["rank"] for r in rows)
    assert ranks == list(range(1, len(rows) + 1))
    live = [r for r in rows if r["live"]]
    assert max(r["rank"] for r in live) == len(live)              # every live claim outranks every dead one


def test_queue_is_rule_flags_plus_top_k_and_never_pads_with_clean_claims(screened):
    rows, _, _ = screened
    q = coding.queue_positions(rows, 30)
    assert all(rows[i]["rule"] or (rows[i]["live"] and rows[i]["rank"] <= 30) for i in q)
    assert {r["pos"] for r in rows if r["rule"]} <= q
    huge = coding.queue_positions(rows, 10_000)
    assert all(rows[i]["live"] or rows[i]["rule"] for i in huge)


def test_planted_upcoding_is_found_far_more_often_than_by_chance(screened, key):
    rows, _, info = screened
    planted = set(key["claims"])
    compared = [r for r in rows if r["tested"] and r["sev"] >= 2]
    q = [rows[i] for i in coding.queue_positions(rows, coding.default_top_k(len(compared)))]
    hit = sum(1 for r in q if r["sep"] in planted)
    chance = len(planted) * len(q) / len(compared)
    assert hit >= 3 * chance


def test_hospital_pattern_separates_upcoding_from_legitimately_sicker_patients(screened, key):
    _, _, info = screened
    verdict = {h["rs"]: h["verdict"] for h in info["hospitals"]}
    roles = key["hospitals"]
    assert "tinjau" in {verdict[rs] for rs, role in roles.items() if role == "strong"}
    assert all(verdict[rs] != "tinjau" for rs, role in roles.items() if role in ("referral", "normal"))
    assert "didukung" in {verdict[rs] for rs, role in roles.items() if role == "referral"}
    assert all(verdict[rs] == "sedikit" for rs, role in roles.items() if role == "tiny")


def test_a_hospital_that_stands_out_does_not_set_the_norm(screened):
    rows, _, info = screened
    flagged = [h["rs"] for h in info["hospitals"] if h["verdict"] in ("tinjau", "didukung")]
    assert flagged
    # peers of a normal hospital exclude the hospitals that stood out, so its z is centred near zero
    normal = [h["z"] for h in info["hospitals"] if h["verdict"] == "wajar" and h["n"] >= coding.MIN_HOSP]
    assert abs(float(np.median(normal))) < 1.0


def test_hospital_table_handles_thin_data():
    assert coding.hospital_table([]) == []
    one = coding.hospital_table([("H1", "C", 2, 0.4, True, False)] * 5)
    assert one[0]["verdict"] == "sedikit" and one[0]["z"] == 0.0 and one[0]["p"] is None


# ---------- API ----------

def test_the_team_workspace_starts_with_the_synthetic_sample(supervisor):
    b = _team_batch(supervisor)
    assert b["labelled"] and b["rowsRead"] == 1087 and b["hospitalsN"] == 24 and not b["smallBase"]
    assert b["planted"] == 38 and b["groups"] == 12 and b["compared"] > 500
    assert b["reviewHospitals"] >= 1 and b["uplift"] > 0
    d = supervisor.get("/api/coding/batches/{}".format(b["id"])).json()
    assert d["claims"] and d["hospitals"] and d["claims"][0]["rank"] == 1
    assert {"tinjau", "didukung", "wajar", "sedikit"} >= {h["verdict"] for h in d["hospitals"]}
    blob = json.dumps(d)
    assert "PASIEN" not in blob and "token" not in blob


def test_coding_claims_store_no_patient_identifiers():
    names = {c.key for c in CodingClaim.__mapper__.column_attrs}
    assert not {n for n in names if any(w in n for w in ("nama", "mrn", "birth", "kartu", "card", "token"))}


def test_claim_detail_explains_on_demand(supervisor):
    b = _team_batch(supervisor)
    claims = supervisor.get("/api/coding/batches/{}".format(b["id"])).json()["claims"]
    c = claims[-1]
    d = supervisor.get("/api/coding/claims/{}".format(c["id"])).json()
    assert d["cbgDesc"].startswith("KASUS SINTETIS") and d["phi"] is not None and len(d["phi"]) == 4
    assert sum(d["phi"]) == pytest.approx(d["score"] - b["baseScore"], abs=1e-6)


def test_capacity_is_a_supervisor_setting_and_is_audited(verifikator, supervisor):
    b = _team_batch(supervisor)
    assert verifikator.patch("/api/coding/batches/{}".format(b["id"]), json={"topK": 10}).status_code == 403
    r = supervisor.patch("/api/coding/batches/{}".format(b["id"]), json={"topK": 25})
    assert r.status_code == 200 and r.json()["topK"] == 25
    big = supervisor.patch("/api/coding/batches/{}".format(b["id"]), json={"topK": 200}).json()
    assert big["topK"] == b["maxTopK"]
    assert supervisor.patch("/api/coding/batches/{}".format(b["id"]), json={"topK": 0}).status_code == 422
    supervisor.patch("/api/coding/batches/{}".format(b["id"]), json={"topK": b["topK"]})
    assert any(a["action"] == "coding_settings" for a in supervisor.get("/api/audit").json())


def test_review_workflow_and_audit(verifikator, supervisor):
    b = _team_batch(verifikator)
    c = verifikator.get("/api/coding/batches/{}".format(b["id"])).json()["claims"][0]
    r = verifikator.put("/api/coding/claims/{}/review".format(c["id"]),
                        json={"status": "docs_requested", "note": "Minta resume medis", "rsResponse": ""})
    assert r.status_code == 200 and r.json()["review"]["status"] == "docs_requested"
    r = verifikator.put("/api/coding/claims/{}/review".format(c["id"]),
                        json={"status": "cleared", "note": "Sesuai rekam medis", "rsResponse": "Resume terlampir"})
    assert r.json()["review"]["rsResponse"] == "Resume terlampir"
    log = [a for a in supervisor.get("/api/audit").json() if a["action"] == "coding_review_set" and a["sep"] == c["sep"]]
    assert len(log) >= 2 and json.loads(log[0]["detail"])["from"] == "docs_requested"
    assert json.loads(log[0]["detail"])["modul"] == "koding"
    r = verifikator.put("/api/coding/claims/{}/review".format(c["id"]), json={"status": None})
    assert r.json()["review"] is None
    assert verifikator.put("/api/coding/claims/{}/review".format(c["id"]), json={"status": "oke"}).status_code == 422


def test_csv_export_neutralises_formulas_and_carries_the_decision(verifikator, supervisor):
    b = _team_batch(verifikator)
    c = verifikator.get("/api/coding/batches/{}".format(b["id"])).json()["claims"][1]
    verifikator.put("/api/coding/claims/{}/review".format(c["id"]),
                    json={"status": "confirmed", "note": "=HYPERLINK(\"x\")"})
    r = verifikator.get("/api/coding/batches/{}/export.csv?k=10".format(b["id"]))
    assert r.status_code == 200 and r.text.startswith("﻿")
    assert "'=HYPERLINK" in r.text and "\n\"=HYPERLINK" not in r.text
    assert "ESTIMATED_TARIFF_DIFFERENCE" in r.text.splitlines()[0]
    verifikator.put("/api/coding/claims/{}/review".format(c["id"]), json={"status": None})


def test_upload_scores_stores_and_deletes_a_new_batch(verifikator, supervisor, text):
    r = verifikator.post("/api/coding/batches", files={"file": ("rs.txt", text.encode(), "text/plain")},
                         data={"name": "Uji unggah"})
    assert r.status_code == 200
    b = r.json()
    assert b["name"] == "Uji unggah" and b["rowsRead"] == 1087 and not b["labelled"] and b["planted"] == 0
    assert any(x["id"] == b["id"] for x in verifikator.get("/api/coding/batches").json())
    assert verifikator.delete("/api/coding/batches/{}".format(b["id"])).status_code == 403
    assert supervisor.delete("/api/coding/batches/{}".format(b["id"])).status_code == 200
    assert supervisor.get("/api/coding/batches/{}".format(b["id"])).status_code == 404
    with SessionLocal() as db:
        assert db.query(CodingClaim).filter_by(batch_id=b["id"]).count() == 0


def test_the_top_up_screen_is_untouched_by_the_coding_tables(supervisor):
    top = supervisor.get("/api/batches").json()
    assert len(top) >= 1 and top[-1]["rowsScreened"] == 80
    assert supervisor.get("/api/coding/batches/{}".format(top[-1]["id"] + 10_000)).status_code == 404


def test_the_page_script_is_served_under_the_csp(server):
    r = server.get("/assets/js/coding.js")
    assert r.status_code == 200 and "VKCoding" in r.text
    assert "script-src 'self'" in server.get("/").headers["content-security-policy"]


# ---------- judge mode ----------

def test_guest_gets_a_private_copy_with_the_same_scores(server, supervisor):
    c = _guest(server)
    mine = c.get("/api/coding/batches").json()
    assert len(mine) == 1 and mine[0]["own"] and mine[0]["labelled"] and mine[0]["reviewed"] == 0
    team = _team_batch(supervisor)
    a = c.get("/api/coding/batches/{}".format(mine[0]["id"])).json()["claims"]
    t = supervisor.get("/api/coding/batches/{}".format(team["id"])).json()["claims"]
    assert [(x["sep"], x["rank"], round(x["score"], 9)) for x in a] == [(x["sep"], x["rank"], round(x["score"], 9)) for x in t]
    assert all(not b["own"] for b in supervisor.get("/api/coding/batches").json())


def test_guests_cannot_see_each_other_or_the_team(server, supervisor):
    a, b = _guest(server), _guest(server)
    a_batch = a.get("/api/coding/batches").json()[0]
    a_claim = a.get("/api/coding/batches/{}".format(a_batch["id"])).json()["claims"][0]
    team = _team_batch(supervisor)
    assert b.get("/api/coding/batches/{}".format(a_batch["id"])).status_code == 404
    assert b.get("/api/coding/claims/{}".format(a_claim["id"])).status_code == 404
    assert b.put("/api/coding/claims/{}/review".format(a_claim["id"]), json={"status": "cleared"}).status_code == 404
    assert b.patch("/api/coding/batches/{}".format(a_batch["id"]), json={"topK": 9}).status_code == 404
    assert b.delete("/api/coding/batches/{}".format(a_batch["id"])).status_code == 404
    assert supervisor.get("/api/coding/batches/{}".format(a_batch["id"])).status_code == 404
    assert a.get("/api/coding/batches/{}".format(team["id"])).status_code == 404


def test_guest_can_decide_set_capacity_upload_and_delete_their_own(server, text, monkeypatch):
    c = _guest(server)
    b = c.get("/api/coding/batches").json()[0]
    claim = c.get("/api/coding/batches/{}".format(b["id"])).json()["claims"][0]
    assert c.put("/api/coding/claims/{}/review".format(claim["id"]), json={"status": "confirmed", "note": "uji"}).status_code == 200
    assert c.patch("/api/coding/batches/{}".format(b["id"]), json={"topK": 14}).json()["topK"] == 14
    monkeypatch.setattr(settings, "guest_max_batches", 2)
    assert c.post("/api/coding/batches", files={"file": ("a.txt", text.encode(), "text/plain")}).status_code == 200
    r = c.post("/api/coding/batches", files={"file": ("a.txt", text.encode(), "text/plain")})
    assert r.status_code == 400 and "Mode juri" in r.json()["detail"]
    log = c.get("/api/audit").json()
    assert {"coding_review_set", "coding_settings", "coding_upload"} <= {a["action"] for a in log}
    assert c.delete("/api/coding/batches/{}".format(b["id"])).status_code == 200


def test_expired_guests_lose_their_coding_batches_too(server):
    c = _guest(server)
    me = c.get("/api/me").json()
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=me["username"]).one()
        user.created_at = utcnow() - timedelta(hours=25)
        db.commit()
    with SessionLocal() as db:
        assert services.purge_guests(db) >= 1
        assert db.query(CodingBatch).filter_by(owner=me["username"]).count() == 0
        assert db.query(CodingBatch).filter_by(owner="system:template").count() == 1


def test_usage_counts_coding_decisions(supervisor, server):
    before = supervisor.get("/api/usage").json()["decisions"]
    c = _guest(server)
    b = c.get("/api/coding/batches").json()[0]
    claim = c.get("/api/coding/batches/{}".format(b["id"])).json()["claims"][0]
    c.put("/api/coding/claims/{}/review".format(claim["id"]), json={"status": "cleared"})
    assert supervisor.get("/api/usage").json()["decisions"] == before + 1


def test_coding_endpoints_need_a_session_and_the_header(server, verifikator):
    anon = TestClient(server.app)
    assert anon.get("/api/coding/batches").status_code == 401
    assert anon.get("/api/coding/claims/1").status_code == 401
    b = _team_batch(verifikator)
    bare = TestClient(server.app)
    bare.cookies.update(verifikator.cookies)
    assert bare.patch("/api/coding/batches/{}".format(b["id"]), json={"topK": 9}).status_code == 403
    assert bare.delete("/api/coding/batches/{}".format(b["id"])).status_code == 403
