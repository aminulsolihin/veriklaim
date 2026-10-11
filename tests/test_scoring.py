"""The parser, the features, the model and the explanations."""
import json

import numpy as np
import pytest

from app import eklaim, scoring
from app.config import settings
from app.security import card_token

REFS = {
    "RR04": {"kind": "SR", "description": "Hip implant / knee implant", "price": 25_000_000},
    "DD01": {"kind": "SD", "description": "Streptokinase", "price": 4_000_000},
}


@pytest.fixture(scope="module")
def screened(sample_text):
    parsed = eklaim.parse_eklaim(sample_text, REFS, card_token)
    model = scoring.screen(parsed["rows"])
    labels = json.loads((settings.data_dir / "sample-labels.json").read_text())
    return parsed, model, labels


def test_sample_parses_completely(screened):
    parsed, _, _ = screened
    assert parsed["total"] == 80 and len(parsed["rows"]) == 80
    assert parsed["no_topup"] == parsed["no_ref"] == parsed["bad"] == 0


def test_parser_never_keeps_patient_identity(sample_text, screened):
    parsed, _, _ = screened
    text = json.dumps(parsed["rows"])
    assert "PASIEN SINTETIS" not in text          # NAMA_PASIEN
    first = sample_text.split("\n")[1].split("\t")
    cols = sample_text.split("\n")[0].split("\t")
    assert first[cols.index("NOKARTU")] not in text   # raw card number
    assert first[cols.index("MRN")] not in text
    assert all(r["card_token"].startswith("P-") for r in parsed["rows"])


def test_bill_rule_and_queue_on_the_sample(screened):
    parsed, _, labels = screened
    rows = parsed["rows"]
    rule = [r for r in rows if r["cover"] < 1]
    top = [r for r in rows if r["rank"] <= scoring.default_top_k(len(rows))]
    flagged = {r["sep"] for r in rule + top}
    assert scoring.default_top_k(80) == 12
    assert len(rule) == 7                                  # deterministic: no model involved
    assert sum(1 for r in rule if r["sep"] in labels) == 7
    assert len(labels) == 14
    assert sum(1 for sep in labels if sep in flagged) == 13    # the one miss is injected to look ordinary
    assert 15 <= len(flagged) <= 16


def test_shapley_values_sum_to_the_score_gap(screened):
    parsed, model, _ = screened
    phi = model.shapley(range(len(parsed["rows"])))
    gap = model.scores - model.base
    assert np.allclose(phi.sum(axis=1), gap, atol=1e-9)
    eager = [r for r in parsed["rows"] if r["rank"] <= scoring.EAGER_EXPLAIN]
    assert all(r["phi"] is not None for r in eager)


def test_refit_gives_the_same_scores(screened):
    parsed, model, _ = screened
    again = scoring.Model(model.X.copy())
    assert np.array_equal(again.scores, model.scores)


def test_reordered_columns_crlf_and_extra_columns(sample_text):
    lines = sample_text.strip().split("\n")
    order = list(range(len(lines[0].split("\t"))))[::-1]
    shuffled = "\r\n".join("\t".join([ln.split("\t")[i] for i in order] + ["x"]) for ln in lines)
    parsed = eklaim.parse_eklaim(shuffled, REFS, card_token)
    assert len(parsed["rows"]) == 80


def test_rows_without_a_priced_topup_are_counted_not_kept(sample_text):
    lines = sample_text.strip().split("\n")
    cols = lines[0].split("\t")
    plain = lines[1].split("\t")
    for k in ("IN_SR", "IN_SD", "SR", "SD"):
        plain[cols.index(k)] = "None"
    other = lines[1].split("\t")
    for k in ("IN_SR", "SR", "IN_SD", "SD"):
        other[cols.index(k)] = "None"
    other[cols.index("IN_SR")] = "RR01"
    parsed = eklaim.parse_eklaim("\n".join(lines + ["\t".join(plain), "\t".join(other)]), REFS, card_token)
    assert (parsed["no_topup"], parsed["no_ref"], len(parsed["rows"])) == (1, 1, 80)


@pytest.mark.parametrize("text, fragment", [
    ("", "tidak memuat baris klaim"),
    ("hello\nworld", "dipisah tab"),
    ("A\tB\tC\tD\tE\tF\tG\tH\tI\tJ\n1\t2", "Kolom tidak ditemukan"),
])
def test_bad_files_give_a_readable_error(text, fragment):
    with pytest.raises(eklaim.EklaimError) as err:
        eklaim.parse_eklaim(text, REFS, card_token)
    assert fragment in str(err.value)
