"""Repeat billing across files: the same SEP or the same card and top-up in an earlier batch of the workspace."""
import csv as csvlib
import json

from fastapi.testclient import TestClient

from app import db as db_module
from app.config import settings
from app.db import Batch, SessionLocal
from tools.make_followup_sample import OUT, generate

from .conftest import HEADERS


def _followup():
    sample = (settings.data_dir / "sample-eklaim.txt").read_text(encoding="utf-8")
    labels = json.loads((settings.data_dir / "sample-labels.json").read_text(encoding="utf-8"))
    return generate(sample, labels)


def _guest(server):
    c = TestClient(server.app)
    c.headers.update(HEADERS)
    assert c.post("/api/demo-login").status_code == 200
    return c, c.get("/api/batches").json()[0]


def _upload(c, text, name="lanjutan.txt"):
    r = c.post("/api/batches", files={"file": (name, text.encode(), "text/plain")})
    assert r.status_code == 200, r.text
    return r.json()


def _subset(text, n):
    lines = text.strip().split("\n")
    return "\n".join(lines[:n + 1]) + "\n"


def test_followup_sample_file_is_up_to_date():
    text, _ = _followup()
    assert OUT.read_text(encoding="utf-8") == text, "run: python tools/make_followup_sample.py"


def test_rebilled_seps_join_the_queue_and_card_repeats_are_counted(server):
    text, key = _followup()
    c, sample = _guest(server)
    b = _upload(c, text)
    assert b["histSep"] == 4 and b["histCard"] == 4 and b["histReexport"] == [] and b["histKeySkipped"] == 0
    claims = {x["sep"]: x for x in c.get("/api/batches/{}".format(b["id"])).json()["claims"]}
    rebilled = key["rebilled_same"] + key["rebilled_changed"]
    assert {s for s, x in claims.items() if x["histBatch"]} == set(rebilled)
    assert all(claims[s]["histBatch"] == sample["id"] and claims[s]["histSepN"] == 1 for s in rebilled)
    # the cross-batch overlap reuses the sample's card and top-up, so it is a card repeat as well
    assert {s for s, x in claims.items() if x["histRep"]} == set(key["card_repeat"] + key["overlap_batch"])
    # queued whatever the settings: one SEP is one episode, whatever its score
    k, thr = b["topK"], b["thresholdPct"]
    normal = sum(1 for x in claims.values()
                 if x["rank"] <= k or x["cover"] * 100 < thr or x["histBatch"] or x["overlapN"])
    assert b["flagged"] == normal
    r = c.patch("/api/batches/{}".format(b["id"]), json={"thresholdPct": 50, "topK": 1})
    assert r.json()["flagged"] >= 4

    same = c.get("/api/claims/{}".format(claims[key["rebilled_same"][0]]["id"])).json()["history"]
    assert len(same["sameSep"]) == 1 and same["sameSep"][0]["batchId"] == sample["id"]
    assert same["sameSep"][0]["tarifRs"] == claims[key["rebilled_same"][0]]["tarifRs"]
    changed = c.get("/api/claims/{}".format(claims[key["rebilled_changed"][0]]["id"])).json()["history"]
    assert changed["sameSep"][0]["tarifRs"] < claims[key["rebilled_changed"][0]]["tarifRs"]
    rep = c.get("/api/claims/{}".format(claims[key["card_repeat"][0]]["id"])).json()["history"]
    assert rep["sameSep"] == [] and len(rep["sameCard"]) == 1 and rep["sameCard"][0]["sep"] != key["card_repeat"][0]

    csv = c.get("/api/batches/{}/export.csv".format(b["id"])).text.lstrip("﻿").strip().split("\r\n")
    assert '"HISTORY_RULE"' in csv[0] and len(csv) - 1 == r.json()["flagged"]
    table = list(csvlib.reader(csv))
    col = table[0].index("HISTORY_RULE")
    assert sum(1 for row in table[1:] if row[col] == "flagged") == 4

    # the earlier batch is gone: the flag stays, the page says the comparison is gone
    assert c.delete("/api/batches/{}".format(sample["id"])).status_code == 200
    gone = c.get("/api/claims/{}".format(claims[rebilled[0]]["id"])).json()["history"]
    assert gone["removed"] and gone["sameSep"] == []


def test_overlapping_stays_join_the_queue_and_readmissions_are_context(server):
    text, key = _followup()
    c, sample = _guest(server)
    b = _upload(c, text)
    assert b["overlap"] == 2 and b["readmit"] == 1
    claims = {x["sep"]: x for x in c.get("/api/batches/{}".format(b["id"])).json()["claims"]}
    assert {s for s, x in claims.items() if x["overlapN"]} == set(key["overlap_file"] + key["overlap_batch"])
    assert {s for s, x in claims.items() if x["readmitDays"]} == set(key["readmit"])

    # in the file: the partner has no top-up, so it is not a stored claim, but it is named on the page
    inner = c.get("/api/claims/{}".format(claims[key["overlap_file"][0]]["id"])).json()
    assert [o["sep"] for o in inner["overlapWith"]] == key["overlap_file_partner"]
    assert inner["overlapWith"][0]["batch"] is None and not inner["overlapWith"][0]["topup"]
    # across batches: the sample's own claim
    cross = c.get("/api/claims/{}".format(claims[key["overlap_batch"][0]]["id"])).json()
    assert [(o["sep"], o["batch"]) for o in cross["overlapWith"]] == [(key["overlap_batch_earlier"][0], sample["id"])]
    again = c.get("/api/claims/{}".format(claims[key["readmit"][0]]["id"])).json()
    assert again["readmitDays"] == 3 and again["readmitWith"]["dx"] == again["dx"]

    # queued whatever the settings
    r = c.patch("/api/batches/{}".format(b["id"]), json={"thresholdPct": 50, "topK": 1}).json()
    queued = {x["sep"] for x in c.get("/api/batches/{}".format(b["id"])).json()["claims"]
              if x["rank"] <= 1 or x["cover"] * 100 < 50 or x["histBatch"] or x["overlapN"]}
    assert r["flagged"] == len(queued) and set(key["overlap_file"] + key["overlap_batch"]) <= queued
    csv = list(csvlib.reader(c.get("/api/batches/{}/export.csv".format(b["id"])).text.lstrip("﻿").strip().split("\r\n")))
    col = csv[0].index("OVERLAP_RULE")
    assert sum(1 for row in csv[1:] if row[col] == "flagged") == 2

    # thalassaemia patients come back by design: the repeat is within the national norm and not queued
    thal = [claims[s] for s in key["thalassaemia"]]
    assert all(x["rep"] == 1 and x["repExp"] > 1 and x["dx"] == "D56" for x in thal)
    assert not any(x["rank"] <= b["topK"] for x in thal)


def test_the_same_file_again_is_a_reexport_not_a_queue_of_duplicates(server, supervisor, sample_text):
    team = supervisor.get("/api/batches").json()
    b = _upload(supervisor, sample_text, "lagi.txt")
    assert b["histSep"] == 0 and b["flagged"] in (15, 16)
    assert {x["id"] for x in b["histReexport"]} == {t["id"] for t in team if t["rowsScreened"] == 80}
    claims = supervisor.get("/api/batches/{}".format(b["id"])).json()["claims"]
    assert all(x["histBatch"] is None and x["histRep"] == 0 for x in claims)
    supervisor.delete("/api/batches/{}".format(b["id"]))


def test_a_part_or_a_superset_of_an_earlier_file_is_a_reexport(server):
    text, _ = _followup()
    c, sample = _guest(server)
    part = _upload(c, _subset(text, 30), "minggu-1.txt")          # the first week alone
    assert part["histSep"] == 0 and part["histReexport"] == []
    whole = _upload(c, text, "bulan.txt")                          # then the whole month, which contains it
    assert [x["id"] for x in whole["histReexport"]] == [part["id"]]
    assert whole["histSep"] == 4                                   # the sample is still compared
    again = _upload(c, _subset(text, 40), "minggu-1-2.txt")        # a part of both earlier uploads
    assert {x["id"] for x in again["histReexport"]} == {part["id"], whole["id"]} and again["histSep"] == 0


def test_history_stays_inside_the_workspace(server, supervisor):
    text, _ = _followup()
    c, sample = _guest(server)
    assert c.delete("/api/batches/{}".format(sample["id"])).status_code == 200
    b = _upload(c, text)                       # the team and other guests have the sample; this guest does not
    assert b["histSep"] == 0 and b["histCard"] == 0
    t = _upload(supervisor, text)               # the team's sample is compared, guests' uploads are not
    assert t["histSep"] == 4
    supervisor.delete("/api/batches/{}".format(t["id"]))


def test_tokens_from_another_secret_key_are_not_compared(server):
    text, _ = _followup()
    c, sample = _guest(server)
    with SessionLocal() as db:
        db.get(Batch, sample["id"]).token_key = "P-OTHERKEY"
        db.commit()
    b = _upload(c, text)
    assert b["histSep"] == 4 and b["histCard"] == 0 and b["histKeySkipped"] == 1


def test_new_columns_are_added_to_an_existing_database():
    db_module.add_missing_columns()   # idempotent on a database that has them
    db_module.add_missing_columns()
