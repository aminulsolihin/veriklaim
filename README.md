# VeriKlaim

Explainable screening of drug and device top-up claims in E-Klaim INA-CBG exports.

VeriKlaim is a prototype built by Team Bill Buster for BPJS Kesehatan Healthkathon 2026, category
*Efisiensi Risiko pada Fasilitas Kesehatan* (sub-categories *Manipulasi Obat & Alkes* and
*Phantom & Repeat Billing*). The interface is in Bahasa Indonesia.

- Live demo: https://veriklaim.onrender.com/ (use **Coba sebagai juri**; no account needed)
- Status: working prototype, tested on synthetic data only
- A flag is a request for a human verifikator to look at a claim. It is not a fraud finding.

## Contents

- [Overview](#overview)
- [How screening works](#how-screening-works)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [API](#api)
- [Testing](#testing)
- [Deployment](#deployment)
- [Operations](#operations)
- [Security and privacy](#security-and-privacy)
- [Evaluation on synthetic data](#evaluation-on-synthetic-data)
- [Project structure](#project-structure)
- [Known limitations](#known-limitations)

## Overview

INA-CBG pays a hospital one package tariff per episode. Special Drugs and Special Prostheses are the
exception: they are billed on top of the package as a top-up, which makes them a natural place to look
for unsupported or repeated charges. VeriKlaim reads a tab-separated E-Klaim export, keeps only the
claims that carry such a top-up, and ranks them for review.

| Area | Capability |
|---|---|
| Ingest | Upload an E-Klaim TXT export. Claims are parsed, scored and stored as a batch. |
| Screening | A bill rule and an Isolation Forest, combined into one review queue. |
| Explanations | Exact Shapley values per claim, with a plain-language reason and the next document to check. |
| Review workflow | Decisions (confirmed, cleared, documents requested), notes, and the hospital's response, stored per claim. |
| Monitoring | Flag rate per hospital and per top-up code, to surface systematic bias. |
| Evaluation | A page with the robustness curve and timing on large files, backed by reproducible scripts. |
| Judge mode | One-click guest sessions, each with a private copy of the synthetic sample and a guided checklist. |
| Governance | Role-based access, an audit log of every state change, CSV export of the queue. |

The interface uses a cream page with flat colour cards. Colour carries meaning: butter marks the bill
rule, mint the anomaly layer, black a claim flagged by both. On phones the navigation becomes a tab bar
and a selected claim opens as a full-screen sheet. Fonts (Plus Jakarta Sans) load from Google Fonts.

## How screening works

1. **Parse.** The reader takes 21 of the 59 `pull_claim` columns (Petunjuk Teknis E-Klaim 5.1.1),
   matched by header name. `NAMA_PASIEN`, `MRN` and `BIRTH_DATE` are never read. `NOKARTU` is replaced
   by a keyed one-way token (HMAC-SHA256) before the row leaves the parser.
2. **Bill rule.** `cover = TARIF_RS / reference price` of the top-up item. A claim is flagged when
   `cover` is below the threshold (default 100%): the hospital's whole bill for the episode is smaller
   than the price of the item it claims.
3. **Anomaly layer.** An Isolation Forest (scikit-learn, 200 trees, `max_samples=256`, fixed seed) over
   four features:
   - bill against reference price (log2 of `cover`)
   - length of stay against the median of the same INA-CBG (log2 ratio)
   - other claims with the same card token and top-up code within 90 days
   - the hospital's top-up volume against its peers (log2 ratio)
4. **Explanation.** Exact Shapley values over the four features (all 16 coalitions, with up to 120
   background claims). The four contributions add up to the difference between the claim's score and
   the batch average. The top 60 ranks are explained at upload; the rest when opened.
5. **Queue.** The union of claims flagged by the bill rule and the top *K* by anomaly score. *K* is a
   reviewer-capacity setting per batch: it defaults to about 15% of screened claims and is capped at 30%
   of them, with a maximum of 40.

Limits: a file needs at least 20 top-up claims, and at most `MAX_CLAIMS` (default 2,000) are screened.
Claims whose top-up code has no reference price are skipped and counted on the batch.

## Quick start

Requirements: Python 3.13 (see `.python-version`).

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

export VERIFIKATOR_PASSWORD=choose-one SUPERVISOR_PASSWORD=choose-another
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 and sign in as `verifikator` or `supervisor`. If the two passwords are not
set, random ones are generated on first start and printed once in the server log. The first start also
loads the synthetic sample batch. In development the OpenAPI docs are at `/api/docs`.

To keep settings in a file, copy `.env.example` to `.env` and start the server with
`uvicorn app.main:app --reload --env-file .env`.

## Configuration

All settings are environment variables, read once at start-up.

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | `production` marks the session cookie `Secure` and hides `/api/docs`. |
| `SECRET_KEY` | random per start | Signs sessions and keys the card tokens. Set it on any real deployment and keep it stable. |
| `DATABASE_URL` | `sqlite:///./veriklaim.db` | SQLite file or a Postgres connection string. `postgres://` and `postgresql://` URLs are accepted. |
| `VERIFIKATOR_PASSWORD`, `SUPERVISOR_PASSWORD` | generated | Passwords for the two accounts. Changing a value and restarting changes the password. |
| `LOGIN_HINT` | empty | Optional text on the sign-in page. |
| `SESSION_HOURS` | `8` | Lifetime of the session cookie, in hours. |
| `SESSION_COOKIE` | `vk_session` | Session cookie name. |
| `MAX_UPLOAD_MB` | `8` | Largest accepted file. |
| `MAX_CLAIMS` | `2000` | Most top-up claims screened per file. |
| `SEED_SAMPLE` | `1` | Load the synthetic sample when the database has no batches. |
| `JUDGE_MODE` | `1` | Enable the guest button and guest sessions. `0` disables them. |
| `GUEST_HOURS` | `24` | Lifetime of a guest session and its data. |
| `MAX_GUESTS` | `300` | Guest sessions alive at once. |
| `GUEST_LOGINS_PER_IP_HOUR` | `60` | New guest sessions per client address per hour. Kept generous because several reviewers can share one office address. |
| `GUEST_MAX_BATCHES` | `5` | Batches one guest may hold. |

## API

JSON over HTTP with a cookie session. Every state-changing request must send
`X-Requested-With: veriklaim`; other origins cannot add that header without a CORS preflight, which
the API never allows.

| Group | Endpoints |
|---|---|
| Health | `GET /healthz` (process only), `GET /readyz` (includes a database check) |
| Session | `POST /api/login`, `POST /api/logout`, `POST /api/demo-login`, `GET /api/me`, `GET /api/config` |
| Batches | `GET`/`POST /api/batches`, `GET`/`PATCH`/`DELETE /api/batches/{id}`, `GET /api/batches/{id}/export.csv` |
| Claims | `GET /api/claims/{id}`, `PUT /api/claims/{id}/review` |
| Reference prices | `GET /api/reference-prices`, `PUT`/`DELETE /api/reference-prices/{code}` |
| Special CMG catalogue | `GET /api/special-cmg` (the 16 codes in Permenkes 26/2021, with whether each has a reference price) |
| Governance | `GET /api/audit`, `GET`/`POST /api/feedback`, `GET /api/feedback.csv`, `GET /api/usage` |
| Evaluation | `GET /api/robustness` |

Roles: `verifikator` reviews claims and uploads files. `supervisor` additionally saves thresholds,
edits reference prices, deletes batches, and reads the audit log and feedback. Guest sessions can act
only on their own data.

## Testing

```bash
pytest                                                                 # SQLite in a temp directory
TEST_DATABASE_URL=postgresql://user@localhost/veriklaim_test pytest    # the same suite on Postgres
```

The suite has 42 tests: the E-Klaim parser, feature and Shapley computation, the API and its role
checks, the review workflow, judge-mode isolation and expiry, and the robustness endpoint.

## Deployment

VeriKlaim runs as one Python web service plus a Postgres database. The reference deployment uses
**Render** for the service and **Neon** for the database, both on free plans and without a payment card.

| Component | Plan | Relevant limits (checked October 2026) |
|---|---|---|
| Render web service | Free | Sleeps after 15 minutes without inbound traffic and takes about a minute to wake. 750 free instance hours per workspace per month. |
| Neon Postgres | Free | Permanent, no card. 1 GB per project, 100 CU-hours per month, compute suspends after 5 minutes idle. |

Use Neon rather than Render's own free Postgres, which expires 30 days after creation.

### Steps

1. Push this directory to a GitHub repository (private is fine).
2. In Neon, create a project (Singapore is the closest region) and copy the connection string, for
   example `postgresql://user:password@ep-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`.
3. In Render choose **New > Blueprint** and select the repository. `render.yaml` defines the web service
   and prompts for the values below.
4. Apply. After the first build, `https://<service>.onrender.com/healthz` should return `{"ok":true}`.

| Variable | Source |
|---|---|
| `DATABASE_URL` | Prompted. The Neon connection string. |
| `VERIFIKATOR_PASSWORD`, `SUPERVISOR_PASSWORD` | Prompted. Passwords for the two team accounts. |
| `LOGIN_HINT` | Prompted, optional. A line shown on the sign-in page. |
| `SECRET_KEY` | Generated by Render. |
| `APP_ENV=production`, `JUDGE_MODE=1`, `GUEST_HOURS=24` | Preset in `render.yaml`. |

`render.yaml` also sets the build and start commands (`uvicorn` with proxy headers enabled) and uses
`/healthz` as the health check.

Keep `SECRET_KEY` unchanged after the first deploy. It signs sessions and keys the card tokens, so
changing it signs everyone out and makes later uploads incomparable with earlier ones.

## Operations

**Keep-alive.** Render and Neon both suspend idle compute. To avoid a one-minute cold start for
visitors during the selection period, schedule an external request to `/healthz` every 10 minutes, for
example with a free job at cron-job.org. `/healthz` does not touch the database, so the pings keep the
web service awake without waking Neon. Neither Render cron jobs nor background workers are available
on the free tier, and the app needs neither: expired guest sessions are purged at start-up and on each
guest sign-in. Continuous pinging uses about 744 of the 750 free instance hours in a 31-day month, so
it fits only when this is the only free web service in the Render workspace.

**Before sharing the link.**
- In a private window, choose **Coba sebagai juri** and complete the 9-step checklist.
- Set `LOGIN_HINT` to a short line for judges, or leave it empty. Judges do not need the team passwords.
- Rotate the two team passwords if they have been shared before.
- Confirm that the scheduler shows successful runs against `/healthz`.

**After the selection.** Set `JUDGE_MODE=0` to disable guest sessions.

## Security and privacy

- Uploaded files reach the server. Use synthetic data, or anonymised data with explicit permission.
  Healthkathon rules prohibit real JKN participant data without it.
- `NAMA_PASIEN`, `MRN` and `BIRTH_DATE` are never read. `NOKARTU` is stored only as a keyed one-way
  token, which still allows repeat top-ups to be counted. The raw file is not retained.
- Passwords are stored as scrypt hashes. Sessions are signed, `HttpOnly`, `SameSite=Lax` cookies
  (`Secure` in production). Failed sign-ins are rate limited, as are new guest sessions per address.
- The service sends `X-Robots-Tag: noindex` and serves `robots.txt`.
- Guests have no password or personal details. The feedback form asks for a role, an optional
  institution and a comment, and tells users not to enter patient data.

This is a prototype with two shared team accounts. Add per-user accounts, password reset and session
revocation before any real use.

## Evaluation on synthetic data

The bundled sample has 80 claims, 14 of which carry an injected problem pattern. At the default
settings the queue holds 16 claims and contains 13 of the 14 patterns. The bill rule alone finds 7, and
a random sample of 16 would contain 2.8 on average. The sample is clean by construction, so two scripts
test harder, larger data. Neither says anything about accuracy on real claims.

```bash
python tools/stress_test.py                 # 20 seeds, 2,000 claims, about 70 s
python tools/benchmark.py 1530000 0.03      # 1.53 million rows, 3% with a top-up
python tools/benchmark.py 1000000 1.0       # 1 million rows, every row a top-up
```

`stress_test.py` inserts eight groups of repeated top-ups at four levels of difficulty, plus about 8%
legitimate repeaters (for example both knees replaced about ten weeks apart). Results at a review
workload of 5% of claims, averaged over 20 seeds:

| Level | Inserted pattern | Recall | Precision | Versus random review |
|---|---|---|---|---|
| L1 | 3 claims, stay of 1 to 2 days (as in the sample) | 100% | 24% | 20× |
| L2 | 2 claims, stay of 2 to 3 days | 97.5% | 15.6% | 19.5× |
| L3 | 2 claims, stay of 3 to 4 days | 82.2% | 13.2% | 16.5× |
| L4 | 2 claims, normal stay (identical to a legitimate repeat) | 22.8% | 3.6% | 4.5× |

At L4 the four features cannot separate the pattern from a legitimate repeat. Catching it requires
matching against the medical record, not a larger model.

`benchmark.py` times parsing, scoring, ranking and explaining, excluding database writes. On a 2 vCPU,
8 GB machine:

| Rows | Share with a top-up | Time | Peak memory |
|---|---|---|---|
| 1,530,000 | 3% | 6.5 s | 1.07 GB |
| 1,000,000 | 100% | 38.7 s | 2.2 GB |

The hosted demo still limits one upload to 2,000 claims. The same figures appear in the app on the
**Ketahanan** page, read from `app/data/robustness.json`. Rerun the scripts and update that file if the
model changes.

## Project structure

```
app/
  main.py          routes, security headers, static serving
  services.py      ingest, explanations, audit entries, start-up seed
  eklaim.py        E-Klaim TXT reader
  scoring.py       features, Isolation Forest, Shapley values
  db.py            SQLAlchemy models
  security.py      password hashing, card tokens, sign-in limiter
  config.py        settings from environment variables
  data/            synthetic sample, its labels, robustness figures, Special CMG code list
  static/          index.html, assets/styles.css, assets/js/app.js
tests/             parser, model, API and judge-mode tests
tools/
  make_sample.js   regenerates the synthetic sample (Node)
  stress_test.py   2,000-claim robustness check with legitimate repeaters
  benchmark.py     read-and-score timing on a large synthetic file
render.yaml        Render Blueprint
.env.example       annotated environment template
```

## Known limitations

- **Reference prices are placeholders.** Only two items have one (hip/knee implant and streptokinase);
  claims with any other top-up are skipped and counted. A supervisor can add prices on the reference
  price page, and a change applies to files uploaded afterwards. Production use needs prices from
  e-Katalog LKPP and e-Fornas.
- **File layout** follows Petunjuk Teknis E-Klaim 5.1.1 (2017). Columns are matched by header name, but
  newer E-Klaim versions are untested, and encrypted TXT is not supported.
- **Special CMG codes.** `RR04` follows the worked example in the Petunjuk Teknis. The Special Drug
  code (`DD01`) and the INA-CBG tariffs in the sample are illustrative.
- **Repeat top-ups** are counted within one file, not across batches.
- **Hospital volume** is a raw count, not scaled to hospital size.
- **Item-level price checks** need per-patient drug and device data from RME-based claims, which E-Klaim
  exports do not carry.
