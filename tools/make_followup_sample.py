"""Make the synthetic follow-up file: "the next month" of the bundled sample, with repeat billing across files.

    python tools/make_followup_sample.py                 # writes app/static/assets/sample/veriklaim-sample-lanjutan.txt
    from tools.make_followup_sample import generate      # generate(sample_text, labels) -> (text, key)

Upload the sample first (every workspace already has it), then this file. Everything is invented:

  next month       every sample claim again, 35 days later, with a new SEP and a new card number
  rebilled         4 ordinary claims of the sample sent again with the same SEP: 2 unchanged, 2 with a higher
                   TARIF_RS (looks like a correction, may be double billing)
  card repeat      3 ordinary claims whose card comes back 35 days later with the same top-up and a new SEP
  overlap, file    1 claim whose stay overlaps a claim without a top-up of the same card at another hospital
  overlap, batch   1 claim with a new SEP whose stay overlaps the same card's stay in the sample (2 days later)
  readmission      1 claim admitted 3 days after a stay of the same card with the same diagnosis ended
  thalassaemia     3 patients on deferiprone (DD02, D56.1), two 2-day stays 28 days apart: routine care

The cross-batch check should put the 4 rebilled SEPs in the queue and count 4 card repeats (the 3 above and
the cross-batch overlap, which reuses the card and top-up). The 2 overlapping claims join the queue; the
readmission is shown as context only. The file shares 4 of its SEPs with the sample, far below the re-export
share, so it is compared and not ignored.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIFT_DAYS = 35
OUT = ROOT / "app" / "static" / "assets" / "sample" / "veriklaim-sample-lanjutan.txt"
TOPUP_COLS = ("SP", "SR", "SI", "SD")


def _day(text: str) -> date:
    d, m, y = (int(x) for x in text.split("/"))
    return date(y, m, d)


def _fmt(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _shift(text: str, days: int = SHIFT_DAYS) -> str:
    return _fmt(_day(text) + timedelta(days=days))


def generate(sample_text: str, labels: dict) -> tuple[str, dict]:
    lines = [ln for ln in sample_text.replace("\r", "").split("\n") if ln.strip()]
    head = lines[0].split("\t")
    ix = {h: i for i, h in enumerate(head)}
    rows = [ln.split("\t") for ln in lines[1:]]
    cards = Counter(r[ix["NOKARTU"]] for r in rows)
    # ordinary claims whose card appears once, so every count below is exact
    plain = [r for r in rows if r[ix["SEP"]] not in labels and cards[r[ix["NOKARTU"]]] == 1]
    rebill, repeat = plain[:4], plain[4:7]
    long_stays = [r for r in plain[7:] if int(r[ix["LOS"]]) >= 4]
    ov_file, ov_batch, readmit = long_stays[0], long_stays[1], long_stays[2]
    repeat_seps = {r[ix["SEP"]] for r in repeat}
    hospitals = sorted({r[ix["KODE_RS"]] for r in rows})

    def no_topup(n: list) -> list:
        for col in TOPUP_COLS:
            n[ix["IN_" + col]] = n[ix[col]] = "None"
            n[ix["DESKRIPSI_" + col]], n[ix["TARIF_" + col]] = "-", "0"
        n[ix["TOTAL_TARIF"]] = n[ix["TARIF_INACBG"]]
        return n

    def stay(n: list, adm: date, los: int) -> None:
        n[ix["ADMISSION_DATE"]], n[ix["DISCHARGE_DATE"]], n[ix["LOS"]] = _fmt(adm), _fmt(adm + timedelta(days=los)), str(los)

    out, shifted = [], {}
    for r in rows:
        n = list(r)
        assert "V0" in n[ix["SEP"]], n[ix["SEP"]]
        n[ix["SEP"]] = n[ix["SEP"]].replace("V0", "V1", 1)
        for col in ("ADMISSION_DATE", "DISCHARGE_DATE"):
            n[ix[col]] = _shift(n[ix[col]])
        if r[ix["SEP"]] not in repeat_seps:
            n[ix["NOKARTU"]] = "8" + n[ix["NOKARTU"]][1:]
        out.append(n)
        shifted[r[ix["SEP"]]] = n
    for i, r in enumerate(rebill):
        n = list(r)
        if i >= 2:
            n[ix["TARIF_RS"]] = str(round(int(r[ix["TARIF_RS"]]) * 1.15))
        out.append(n)

    # overlap in the file: during the stay, a claim without a top-up for the same card at another hospital
    top = shifted[ov_file[ix["SEP"]]]
    partner = no_topup(list(top))
    partner[ix["SEP"]] = top[ix["SEP"]].replace("V1", "V2", 1)
    partner[ix["KODE_RS"]] = next(h for h in hospitals if h != top[ix["KODE_RS"]])
    partner[ix["DIAGLIST"]], partner[ix["PROCLIST"]] = "J18.9", "-"
    partner[ix["INACBG"]], partner[ix["DESKRIPSI_INACBG"]] = "J-4-16-I", "INFEKSI DAN RADANG SALURAN NAPAS (RINGAN)"
    stay(partner, _day(top[ix["ADMISSION_DATE"]]) + timedelta(days=1), 2)
    out.append(partner)

    # overlap across batches: the sample's card and top-up again, new SEP, admitted 2 days into the sample's stay
    again = list(ov_batch)
    again[ix["SEP"]] = ov_batch[ix["SEP"]].replace("V0", "V3", 1)
    stay(again, _day(ov_batch[ix["ADMISSION_DATE"]]) + timedelta(days=2), int(ov_batch[ix["LOS"]]))
    out.append(again)

    # readmission: a stay with the same diagnosis and card that ended 3 days before this claim's admission
    top = shifted[readmit[ix["SEP"]]]
    before = no_topup(list(top))
    before[ix["SEP"]] = top[ix["SEP"]].replace("V1", "V4", 1)
    stay(before, _day(top[ix["ADMISSION_DATE"]]) - timedelta(days=6), 3)
    out.append(before)

    # thalassaemia on deferiprone: two 2-day stays 28 days apart, three patients
    template = next(r for r in plain[10:] if r[ix["IN_SD"]] == "DD01")
    thal = []
    for k in range(3):
        for visit in range(2):
            n = list(template)
            n[ix["SEP"]] = "9990R{}0526V5{:05d}".format(hospitals[k][-3:], 2 * k + visit + 1)
            n[ix["NOKARTU"]] = "777000000{:04d}".format(k + 1)
            n[ix["KODE_RS"]] = hospitals[k]
            n[ix["DIAGLIST"]], n[ix["PROCLIST"]] = "D56.1", "99.04"
            n[ix["INACBG"]], n[ix["DESKRIPSI_INACBG"]] = "D-4-13-I", "GANGGUAN SEL DARAH MERAH (RINGAN)"
            n[ix["IN_SD"]], n[ix["SD"]], n[ix["DESKRIPSI_SD"]], n[ix["TARIF_SD"]] = "DD02", "DD-02-I", "DEFERIPRONE", "2500000"
            n[ix["TARIF_INACBG"]] = "3100000"
            n[ix["TOTAL_TARIF"]] = "5600000"
            n[ix["TARIF_RS"]] = str(6_400_000 + 150_000 * (2 * k + visit))
            n[ix["DISCHARGE_STATUS"]], n[ix["ICU_INDIKATOR"]], n[ix["ICU_LOS"]], n[ix["VENT_HOUR"]] = "1", "0", "0", "0"
            stay(n, date(2026, 5, 6) + timedelta(days=3 * k + 28 * visit), 2)
            out.append(n)
            thal.append(n[ix["SEP"]])

    key = {
        "rebilled_same": [r[ix["SEP"]] for r in rebill[:2]],
        "rebilled_changed": [r[ix["SEP"]] for r in rebill[2:]],
        "card_repeat": [r[ix["SEP"]].replace("V0", "V1", 1) for r in repeat],
        "overlap_file": [shifted[ov_file[ix["SEP"]]][ix["SEP"]]],
        "overlap_file_partner": [partner[ix["SEP"]]],
        "overlap_batch": [again[ix["SEP"]]],
        "overlap_batch_earlier": [ov_batch[ix["SEP"]]],
        "readmit": [shifted[readmit[ix["SEP"]]][ix["SEP"]]],
        "thalassaemia": thal,
    }
    return "\n".join(["\t".join(head)] + ["\t".join(n) for n in out]) + "\n", key


def main() -> None:
    data = ROOT / "app" / "data"
    text, key = generate((data / "sample-eklaim.txt").read_text(encoding="utf-8"),
                         json.loads((data / "sample-labels.json").read_text(encoding="utf-8")))
    OUT.write_text(text, encoding="utf-8", newline="\n")
    json.dump(key, sys.stdout, indent=2)
    print("\nwrote", OUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
