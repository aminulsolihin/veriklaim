"""Record rules on episodes of care (episodes.py): overlapping stays and quick same-diagnosis readmissions."""
from app import episodes
from app.eklaim import parse_eklaim


def ep(sep, adm, dis, card="A", dx="I21", inpatient=True, batch=None, topup=False):
    return {"sep": sep, "card_token": card, "adm_day": adm, "dis_day": dis, "inpatient": inpatient, "dx3": dx,
            "kode_rs": "H1", "adm_date": str(adm), "dis_date": str(dis), "topup": topup, "batch": batch}


def row(sep, card="A"):
    return {"sep": sep, "card_token": card}


def test_overlap_definition():
    assert episodes.overlaps(1, 5, 3, 8)            # shares nights
    assert episodes.overlaps(1, 5, 1, 2)            # admitted the same day
    assert not episodes.overlaps(1, 5, 5, 8)        # admitted the day the other ended: a transfer
    assert not episodes.overlaps(1, 5, 6, 8)
    assert episodes.overlaps(1, 5, 3, 3)            # a same-day claim inside the stay
    assert not episodes.overlaps(1, 5, 1, 1)        # on the admission day (for example the emergency visit)
    assert not episodes.overlaps(1, 5, 5, 5)        # on the discharge day
    assert not episodes.overlaps(3, 3, 3, 3)        # two same-day claims


def test_annotate_overlap_readmission_and_transfer():
    eps = [ep("S1", 10, 15), ep("S2", 12, 14, dx="J18"),          # overlap
           ep("S3", 15, 18, dx="K35"),                             # transfer on S1's discharge day: no overlap
           ep("S4", 30, 33), ep("S5", 36, 40),                     # S5 readmitted 3 days after S4, same diagnosis
           ep("S6", 50, 52, dx="J18"), ep("S7", 54, 56),           # 2 days after a stay with another diagnosis
           ep("S8", 12, 14, card="B")]                             # another card
    rows = [row(e["sep"], e["card_token"]) for e in eps]
    out = episodes.annotate(rows, eps)
    by = {r["sep"]: r for r in rows}
    assert {s for s, r in by.items() if r["overlap_n"]} == {"S1", "S2"}
    assert by["S1"]["overlap_with"][0]["sep"] == "S2" and by["S2"]["overlap_with"][0]["sep"] == "S1"
    assert by["S5"]["readmit_days"] == 3 and by["S5"]["readmit_with"]["sep"] == "S4"
    assert by["S7"]["readmit_days"] is None and by["S8"]["overlap_n"] == 0
    assert out == {"overlap": 2, "readmit": 1}


def test_earlier_batches_count_and_the_same_sep_does_not():
    eps = [ep("S1", 10, 15)]
    earlier = [ep("S1", 10, 15, batch=1),          # the same SEP: the history rule's business, not an overlap
               ep("E1", 13, 16, batch=1, topup=True)]
    rows = [row("S1")]
    episodes.annotate(rows, eps, earlier)
    assert rows[0]["overlap_n"] == 1 and rows[0]["overlap_with"][0] == {
        "sep": "E1", "rs": "H1", "adm": "13", "dis": "16", "dx": "I21", "topup": True, "batch": 1}


def test_parser_keeps_claims_without_topup_of_the_same_card():
    head = ["KODE_RS", "KODE_TARIF", "ADMISSION_DATE", "DISCHARGE_DATE", "INACBG", "TARIF_RS", "LOS", "SEP", "NOKARTU",
            "DIAGLIST", "IN_SR", "IN_SD", "TARIF_SR", "TARIF_SD"]
    lines = [
        ["R1", "C", "01/04/2026", "06/04/2026", "M-1-04-II", "60000000", "5", "S1", "111", "M17.1", "RR04", "None", "25000000", "0"],
        ["R2", "C", "03/04/2026", "04/04/2026", "J-4-16-I", "3000000", "1", "S2", "111", "J18.9", "None", "None", "0", "0"],
        ["R2", "C", "03/04/2026", "04/04/2026", "J-4-16-I", "3000000", "1", "S3", "999", "J18.9", "None", "None", "0", "0"],
    ]
    text = "\n".join("\t".join(x) for x in [head] + lines)
    refs = {"RR04": {"kind": "SR", "description": "Hip", "price": 25_000_000}}
    p = parse_eklaim(text, refs, lambda s: "T" + s)
    assert [r["sep"] for r in p["rows"]] == ["S1"]
    assert [(e["sep"], e["topup"]) for e in p["episodes"]] == [("S1", True), ("S2", False)]   # card 999 has no top-up
    assert all(e["card_token"] in ("T111",) for e in p["episodes"])
    episodes.annotate(p["rows"], p["episodes"])
    assert p["rows"][0]["overlap_n"] == 1
