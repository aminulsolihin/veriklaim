"""National diagnosis norms (baseline.py) and how the top-up features use them."""
from app import baseline, scoring


def _row(i, diag, cbg, card, day, los=2, code="DD02"):
    return {"sep": f"S{i}", "kode_rs": "H1", "cmg_code": code, "inacbg": cbg, "diag": diag, "los": los,
            "tarif_rs": 9_000_000, "ref_price": 3_000_000, "card_token": card, "adm_day": day}


def test_primary_dx_and_setting():
    assert baseline.primary_dx("I21.0#I10") == "I21"
    assert baseline.primary_dx(" d56.1;D64.9") == "D56"
    assert baseline.primary_dx("") == "" and baseline.primary_dx("-") == ""
    assert baseline.is_inpatient("I-4-10-III", 0)       # case type 4: inpatient, whatever LOS says
    assert not baseline.is_inpatient("Q-5-44-0", 3)     # case type 5: outpatient
    assert baseline.is_inpatient("", 2) and not baseline.is_inpatient("", 0)


def test_baseline_file_has_the_topup_diagnoses():
    d = baseline.load()
    for dx in ("D56", "I21", "M17", "A41"):
        assert dx in d["inpatient"] and d["inpatient"][dx]["n"] >= d["min_n"]
    # the reason the file exists: thalassaemia patients come back, MI patients mostly do not
    assert d["inpatient"]["D56"]["near_mean"] > 10 * d["inpatient"]["I21"]["near_mean"]


def test_expected_repeats_offset_chronic_diagnoses_only():
    rows = []
    for k in range(3):  # three claims 30 days apart: one thalassaemia patient, one MI patient
        rows.append(_row(len(rows), "D56.1", "D-4-13-I", "THAL", 30 * k))
        rows.append(_row(len(rows), "I21.0", "I-4-10-II", "MI", 30 * k, code="DD01"))
    for i in range(20):
        rows.append(_row(len(rows), "J18.9", "J-4-16-I", f"P{i}", i * 3, los=4))
    X = scoring.featurize(rows)
    thal = [i for i, r in enumerate(rows) if r["card_token"] == "THAL"]
    mi = [i for i, r in enumerate(rows) if r["card_token"] == "MI"]
    assert all(rows[i]["repeat_n"] == 2 for i in thal + mi)
    assert all(rows[i]["repeat_exp"] > 1.0 for i in thal)
    assert all(rows[i]["repeat_exp"] < 0.2 for i in mi)
    assert max(X[i, 2] for i in thal) < min(X[i, 2] for i in mi)
    assert all(r["dx3"] for r in rows)


def test_unknown_diagnosis_scores_as_before():
    rows = [_row(i, "", "M-1-04-I", f"P{i // 2}", i * 10, code="RR04") for i in range(20)]
    X = scoring.featurize(rows)
    assert all(r["repeat_exp"] == 0.0 and r["los_nat"] is None for r in rows)
    assert all(X[i, 2] == rows[i]["repeat_n"] for i in range(len(rows)))
    # stored claims from before this change have repeat_exp 0, so their matrix is rebuilt unchanged
    assert scoring.feature_vector(2.0, 1.0, 3, 1.0) == scoring.feature_vector(2.0, 1.0, 3, 1.0, 0.0)


def test_small_group_stay_median_moves_towards_national():
    rows = [_row(0, "I21.0", "I-4-10-II", "A", 0, los=20, code="DD01")]
    rows += [_row(i, "J18.9", "J-4-16-I", f"P{i}", i, los=4) for i in range(1, 25)]
    scoring.featurize(rows)
    nat = rows[0]["los_nat"]
    assert nat is not None and nat < 20
    assert nat < rows[0]["los_median"] < 20          # one claim of 20 days, pulled towards the national median
