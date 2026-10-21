"""Hostile or awkward input, the rate-limit key, and the CSV export of numbers."""
import math

import pytest
from fastapi.testclient import TestClient

from app import coding, eklaim, main, security
from app.config import settings
from tests.conftest import HEADERS

HDR = "KODE_RS\tKODE_TARIF\tADMISSION_DATE\tINACBG\tTARIF_RS\tLOS\tSEP\tNOKARTU\tIN_SR\tIN_SD\tTARIF_SR\tTARIF_SD\tTARIF_INACBG\tTOTAL_TARIF"
REFS = {"RR04": {"kind": "SR", "description": "Hip implant", "price": 25_000_000}}


def _row(sep="SEP1", tarif="20000000", los="3"):
    return "9990001\tX\t01/04/2026\tI-4-10-II\t{}\t{}\t{}\t111\tRR04\t-\t0\t0\t0\t0".format(tarif, los, sep)


@pytest.mark.parametrize("text,want", [
    ("25000000", 25_000_000), ("25000000.50", 25_000_000.5), ("25,000,000.50", 25_000_000.5),
    ("25.000.000,50", 25_000_000.5), ("25000000,50", 25_000_000.5), ("25.000", 25_000), ("25,000", 25_000),
    ("1,5", 1.5), ("0.5", 0.5), ("-3", -3), ("Rp 1.250.000", 1_250_000), ("", 0), ("abc", 0), ("1-2", 0),
])
def test_numbers_are_read_whichever_way_they_are_written(text, want):
    assert eklaim.to_number(text) == pytest.approx(want)


@pytest.mark.parametrize("text", ["9" * 400, "9" * 30, "1" + "0" * 14])
def test_absurd_numbers_are_clamped_not_fatal(text):
    assert eklaim.to_number(text) == eklaim.MAX_NUMBER
    assert math.isfinite(eklaim.to_number(text))


def test_an_absurd_amount_drops_the_row_instead_of_failing_the_upload():
    p = eklaim.parse_eklaim("\n".join([HDR, _row("A", tarif="9" * 400), _row("B")]), REFS, lambda s: s)
    assert [r["sep"] for r in p["rows"]] == ["B"] and p["bad"] == 1


def test_coding_reader_survives_an_absurd_stay():
    head = "KODE_RS\tSEP\tINACBG\tLOS\tDIAGLIST\tA\tB\tC\tD\tE"
    p = coding.parse_coding("\n".join([head, "1\tS1\tI-4-10-II\t{}\tA01#B02\t\t\t\t\t".format("9" * 400)]))
    assert len(p["rows"]) == 1 and p["rows"][0]["los"] == 100_000


def test_an_absurd_stay_in_the_topup_reader_is_clamped_to_fit_the_column():
    p = eklaim.parse_eklaim("\n".join([HDR, _row("A", los="9" * 400)]), REFS, lambda s: s)
    assert p["rows"][0]["los"] == 100_000


def test_a_repeated_sep_is_kept_once_in_both_readers():
    p = eklaim.parse_eklaim("\n".join([HDR, _row("A"), _row("A"), _row("B")]), REFS, lambda s: s)
    assert [r["sep"] for r in p["rows"]] == ["A", "B"] and p["dup"] == 1 and p["bad"] == 1
    head = "KODE_RS\tSEP\tINACBG\tLOS\tDIAGLIST\tA\tB\tC\tD\tE"
    line = "1\t{}\tI-4-10-II\t3\tA01#B02\t\t\t\t\t"
    q = coding.parse_coding("\n".join([head, line.format("S1"), line.format("S1")]))
    assert len(q["rows"]) == 1 and q["dup"] == 1


def test_csv_keeps_numbers_numeric_and_still_blocks_formulas():
    assert main._csv_cell("-0.0123") == "-0.0123"
    assert main._csv_cell("42") == "42"
    for bad in ("=1+1", "+cmd", "-cmd|x", "@SUM(A1)", "-1+1"):
        assert main._csv_cell(bad).startswith("'"), bad


def test_limiter_stores_only_failing_keys_and_sweeps_expired_ones(monkeypatch):
    lim = security.LoginLimiter(limit=2, window=10)
    for i in range(500):
        assert lim.blocked("k%d" % i) is False
    assert len(lim._fails) == 0
    lim.fail("a"); lim.fail("a")
    assert lim.blocked("a") and not lim.blocked("b")
    clock = [1000.0]
    monkeypatch.setattr(security.time, "monotonic", lambda: clock[0])
    lim = security.LoginLimiter(limit=2, window=10)
    for i in range(lim._SWEEP_AT):
        lim.fail("old%d" % i)
    clock[0] += 11
    lim.fail("new")
    assert len(lim._fails) == 1 and not lim.blocked("old1")
    lim.reset("new")
    assert len(lim._fails) == 0


class _Req:
    def __init__(self, xff=None, host="10.0.0.9"):
        self.headers = {"x-forwarded-for": xff} if xff is not None else {}
        self.client = type("C", (), {"host": host})()


def test_client_ip_ignores_what_the_client_wrote(monkeypatch):
    monkeypatch.setattr(settings, "proxy_hops", 2)
    # Render adds its own proxy after the client: "<spoofed...>, <client>, <render proxy>"
    assert security.client_ip(_Req("6.6.6.6, 203.0.113.9, 10.1.1.1")) == "203.0.113.9"
    assert security.client_ip(_Req("203.0.113.9, 10.1.1.1")) == "203.0.113.9"
    assert security.client_ip(_Req("only-one")) == "10.0.0.9"     # too short to trust: socket address
    assert security.client_ip(_Req()) == "10.0.0.9"
    monkeypatch.setattr(settings, "proxy_hops", 1)
    assert security.client_ip(_Req("6.6.6.6, 203.0.113.9")) == "203.0.113.9"
    monkeypatch.setattr(settings, "proxy_hops", 0)
    assert security.client_ip(_Req("6.6.6.6, 203.0.113.9")) == "10.0.0.9"


def test_rotating_the_forwarded_for_header_does_not_escape_the_login_limit(server, monkeypatch):
    monkeypatch.setattr(settings, "proxy_hops", 2)
    monkeypatch.setattr(main, "login_limiter", security.LoginLimiter(limit=3, window=600))
    c = TestClient(server.app)
    codes = []
    for i in range(6):
        h = dict(HEADERS, **{"X-Forwarded-For": "9.9.9.%d, 203.0.113.50, 10.1.1.1" % i})
        codes.append(c.post("/api/login", json={"username": "supervisor", "password": "wrong"}, headers=h).status_code)
    assert codes == [401, 401, 401, 429, 429, 429]


def test_rotating_the_forwarded_for_header_does_not_escape_the_guest_cap(server, monkeypatch):
    monkeypatch.setattr(settings, "proxy_hops", 2)
    monkeypatch.setattr(main, "guest_limiter", security.LoginLimiter(limit=2, window=3600))
    c = TestClient(server.app)
    codes = []
    for i in range(3):
        h = dict(HEADERS, **{"X-Forwarded-For": "8.8.8.%d, 203.0.113.77, 10.1.1.1" % i})
        codes.append(c.post("/api/demo-login", headers=h).status_code)
    assert codes == [200, 200, 429]


def test_negative_shapley_values_export_as_numbers(verifikator):
    batches = verifikator.get("/api/batches").json()
    b = next(x for x in batches if x["labelled"])
    csv_text = verifikator.get("/api/batches/{}/export.csv?view=all".format(b["id"])).text
    assert "\"'-0." not in csv_text and "\"-0." in csv_text
