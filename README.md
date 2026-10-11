# VeriKlaim MVP

Prototype for BPJS Kesehatan Healthkathon 2026, Team Bill Buster.

A full-stack web app that screens obat and alkes top-up claims in an E-Klaim INA-CBG
TXT export and gives verifikator a saved review queue. The interface is in Bahasa Indonesia
and matches the pitch deck: every number the deck quotes for the synthetic sample appears in the app.

## What it does

| Part | What happens |
|---|---|
| Upload | A plain E-Klaim TXT file is sent to the server, parsed, scored and stored as a batch. |
| Bill rule | `TARIF_RS` divided by the reference price of the Special Drug or Special Prosthesis item. |
| Anomaly layer | A scikit-learn Isolation Forest (200 trees) over four features computed from the file. |
| Explanations | Exact Shapley values per claim, with a plain-language reason and next check. |
| Review | A verifikator records a decision and note on each claim. When documents are requested, the hospital's response is recorded on the same claim. Decisions are stored. |
| Comparison | On the synthetic sample, the page shows what the queue catches against a random sample of the same size (13 vs 2.8 of 14 at the default settings). |
| Bias monitoring | Flag rate per hospital and per top-up code, with confirmed, cleared and awaiting-documents counts. A tag marks rates at least twice the batch average. |
| Interface | Cream sheet with flat colour cards. Colour carries meaning: butter is the bill rule, mint the anomaly layer, black a claim flagged by both. On phones the menu becomes a floating tab bar, the queue is a list of cards, and a tapped claim opens as a full-screen sheet with a back button. Fonts load from Google Fonts (Plus Jakarta Sans). |
| Robustness page | "Ketahanan": the curve of patterns caught against review workload on 2,000 synthetic claims that include legitimate repeaters, and the timing on large files. Figures come from `app/data/robustness.json`, written from the two scripts under `tools/`. |
| Reference-price notice | While any reference price is marked as a placeholder, the queue says so. The bundled prices are illustrative, not e-Katalog data. |
| Judge mode | A "Coba sebagai juri" button on the sign-in page. Each visitor gets a private, already-scored copy of the synthetic sample, a 9-step guided checklist, and their own audit trail. Guests cannot change reference prices or see anyone else's data, and are removed after 24 hours. |
| Feedback and usage | A one-minute feedback form for anyone who tries the app. A supervisor sees the answers, exports them as CSV, and sees usage counts from the audit log (judge sessions, decisions, uploads). |
| Roles | `verifikator` reviews and uploads. `supervisor` also saves thresholds, edits reference prices, deletes batches and reads the audit log. |
| Audit log | Sign-ins, uploads, threshold changes, decisions and hospital responses, reference-price edits and exports. |
| Export | The queue with scores, drivers and decisions, as CSV. |

Stack: Python 3.13, FastAPI, SQLAlchemy, scikit-learn, and a plain HTML/CSS/JavaScript
front end served by the same app. SQLite locally, Postgres when hosted.

## Run it on your computer

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

export VERIFIKATOR_PASSWORD=choose-one SUPERVISOR_PASSWORD=choose-another
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 and sign in as `verifikator` or `supervisor`.
If you do not set the two passwords, random ones are generated on the first start and
printed once in the terminal. The first start also loads the synthetic sample batch.

In development the API is documented at http://127.0.0.1:8000/api/docs.

Run the tests:

```bash
pytest
TEST_DATABASE_URL=postgresql://user@localhost/veriklaim_test pytest   # same tests on Postgres
```

## Put it online for free

The app is one Python server plus a Postgres database. Free options checked in October 2026:

| Route | Card needed | Notes |
|---|---|---|
| **Render + Neon** (recommended) | No | Render runs the app; Neon keeps the data. The app sleeps after 15 minutes without traffic and takes up to about a minute to wake. |
| Firebase Hosting + Cloud Run + Neon | Yes (Blaze plan) | Firebase's free Spark plan cannot run a Python server. Cloud Run has a monthly free allowance, so a demo normally costs nothing, but a billing account must be linked. |
| Heroku | — | No free tier since November 2022. |

Render's own free Postgres is deleted 30 days after it is created, so do not use it for the
competition period. Neon's free plan does not expire (1 GB per project, no card).

### Route A: Render + Neon

1. Put this folder in a GitHub repository (private is fine).
2. At https://neon.com create a project (region: Singapore is closest) and copy its connection
   string, which looks like `postgresql://user:password@ep-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`.
3. At https://render.com sign in with GitHub, choose **New > Blueprint** and pick the repository.
   Render reads `render.yaml` and asks for:
   - `DATABASE_URL`: the Neon connection string
   - `VERIFIKATOR_PASSWORD` and `SUPERVISOR_PASSWORD`: passwords for the two demo accounts
   - `LOGIN_HINT`: optional text on the sign-in page, for example the demo accounts for judges
4. Choose **Apply**. The first build takes a few minutes. The address is
   `https://<service-name>.onrender.com`; check `https://<service-name>.onrender.com/healthz`
   returns `{"ok":true}`.

`SECRET_KEY` is generated by Render. Keep it unchanged: it signs sessions and derives the card
tokens, so changing it signs everyone out and changes the tokens of later uploads.

Before a judging session, open the address once a minute or two ahead so the server is awake.

### Keep it awake during the selection period

Judges may open the link at any hour, and a sleeping free server takes up to a minute to wake.
Ping it every 10 minutes so it stays awake:

1. At https://cron-job.org (free) create a cron job for `https://<service-name>.onrender.com/healthz`,
   every 10 minutes.
2. That is the only free web service in the Render workspace, so it fits Render's 750 free hours a
   month (a 31-day month is 744 hours). A second free service in the same workspace would not fit.

`/healthz` does not touch the database, so the pings do not keep Neon's compute running and use
none of its 100 free compute hours. Neon only wakes when someone actually uses the app.
`/readyz` checks the database too, for manual checks.

### Before you submit the link

- Open the address in a private window, choose **Coba sebagai juri**, and walk the 8 checklist steps.
- Set `LOGIN_HINT` to a short line for judges, or leave it empty: judges do not need the team passwords.
- Change the two team passwords from anything you have shared before.
- Check `/healthz` from the cron service shows successful runs.
- After the selection, set `JUDGE_MODE=0` to switch the button off.

### Route B: Firebase Hosting in front of Cloud Run

Use this only if you want a `web.app` address. It needs the Blaze plan (a linked billing account).

1. Create a Firebase project, upgrade it to Blaze, and set a budget alert (for example USD 1)
   in Google Cloud Billing.
2. Install the Google Cloud CLI and the Firebase CLI, then sign in to both.
3. Deploy the server to Cloud Run from this folder (it builds from the `Dockerfile`):

   ```bash
   gcloud run deploy veriklaim --source . --region us-central1 --allow-unauthenticated \
     --memory 512Mi --max-instances 1 \
     --set-env-vars APP_ENV=production,SESSION_COOKIE=__session,SECRET_KEY=<long-random>,DATABASE_URL=<neon-url>,VERIFIKATOR_PASSWORD=<pw>,SUPERVISOR_PASSWORD=<pw>
   ```

   `SESSION_COOKIE=__session` is required: Firebase Hosting removes every other cookie before a
   request reaches Cloud Run, so sign-in would not stick without it.
4. In the `firebase/` folder run `firebase use --add` (pick the project), then `firebase deploy --only hosting`.
   `firebase.json` sends every path to the Cloud Run service. The address is `https://<project-id>.web.app`.

The `Dockerfile` was not build-tested in the environment where this prototype was made.

### Other hosts

Any host that runs a container can use the `Dockerfile`. Without a `DATABASE_URL` the app uses a
SQLite file, which only survives restarts on a host with a persistent disk.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `APP_ENV` | `development` | `production` marks the session cookie Secure and hides `/api/docs`. |
| `SECRET_KEY` | random per start | Signs sessions and derives card tokens. Set it on any real deployment. |
| `DATABASE_URL` | `sqlite:///./veriklaim.db` | SQLite file or a Postgres connection string. |
| `VERIFIKATOR_PASSWORD`, `SUPERVISOR_PASSWORD` | generated | Passwords of the two accounts. Changing a value and restarting changes the password. |
| `LOGIN_HINT` | empty | Text shown on the sign-in page. |
| `MAX_UPLOAD_MB` | `8` | Largest file accepted. |
| `MAX_CLAIMS` | `2000` | Most top-up claims screened per file. |
| `SEED_SAMPLE` | `1` | Load the synthetic sample when the database has no batches. |
| `JUDGE_MODE` | `1` | Show "Coba sebagai juri" and allow guest sessions. `0` turns it off. |
| `GUEST_HOURS` | `24` | How long a guest session and its data last. |
| `MAX_GUESTS` | `300` | Most guest sessions alive at once. |
| `GUEST_LOGINS_PER_IP_HOUR` | `10` | New guest sessions per network per hour. |
| `GUEST_MAX_BATCHES` | `5` | Batches a guest may hold. |
| `SESSION_COOKIE` | `vk_session` | Name of the session cookie. Set `__session` behind Firebase Hosting. |

## Privacy and data

- **Uploaded files now go to the server.** Use synthetic data, or anonymised data you have
  official permission to use. Healthkathon rules forbid real JKN participant data without it.
- `NAMA_PASIEN`, `MRN` and `BIRTH_DATE` are never read.
- `NOKARTU` is replaced by a one-way keyed token before anything is stored, so repeat
  top-ups can still be counted. The raw number is not stored.
- The raw file is not kept. Only the screened claims and their features are stored.
- Passwords are stored as scrypt hashes. Sessions are signed, HttpOnly, SameSite=Lax
  cookies. Changes need a request header that other sites cannot send.
- `robots.txt` and a `noindex` header keep the site out of search engines.
- Judge-mode guests have no password and no personal details. The feedback form asks for a role,
  an optional institution and a comment, and asks people not to write patient data.

This is a prototype: there are two shared accounts and no password reset, per-user accounts
or session revocation. Add those before any real use.

## Folder map

```
app/
  main.py        API routes and web app
  services.py    ingest a file, explain a claim, audit entries, start-up seed
  eklaim.py      E-Klaim TXT reader
  scoring.py     features, Isolation Forest, Shapley values
  db.py          tables
  security.py    password hashing, card tokens, sign-in limiter
  config.py      settings from environment variables
  data/          synthetic sample, its labels, robustness figures
  static/        index.html, styles.css, js/app.js
tests/           parser, model and API tests
tools/make_sample.js   regenerates the synthetic sample (Node)
tools/stress_test.py   2,000-claim robustness check with legitimate repeaters (synthetic)
tools/benchmark.py     read-and-score timing on a large synthetic file
render.yaml      Render Blueprint
firebase/        Firebase Hosting config for Route B
Dockerfile       for container hosts
```

## Robustness and scale checks (synthetic)

The 80-claim sample is clean by construction, so two scripts check the model on harder, larger data.
Both use synthetic data only; neither says anything about accuracy on real claims.

```bash
python tools/stress_test.py                 # 20 seeds, 2,000 claims, about 70 s
python tools/benchmark.py 1530000 0.03      # 1.53 million rows, 3% with a top-up
python tools/benchmark.py 1000000 1.0       # 1 million rows, every row a top-up
```

`stress_test.py` inserts eight groups of repeated top-ups at four levels of difficulty (L1 looks like the
sample, L4 is identical to a legitimate repeat) and about 8% legitimate repeaters (for example both knees
replaced about ten weeks apart). With the default settings, reviewing 5% of claims catches 100%, 97.5%, 82.2%
and 22.8% of the inserted patterns at L1 to L4, with precision 24%, 15.6%, 13.2% and 3.6%
(20, 19.5, 16.5 and 4.5 times a random review). At L4 the four features cannot tell the pattern from a legitimate
repeat; catching it needs the medical record. `benchmark.py` times parsing, scoring, ranking and explaining
(not database writes): on a 2 vCPU, 8 GB machine, 6.5 s and 1.07 GB for 1.53 million rows with 3% top-ups,
and 38.7 s and 2.2 GB for 1 million rows that all have a top-up. The hosted demo still limits an upload to
2,000 claims. The same figures are shown in the app on the Ketahanan page, read from
`app/data/robustness.json`; rerun the scripts and update that file if you change the model.

## Things to change before real use

- **Reference prices** are placeholders. Only two items have one (hip/knee implant and
  streptokinase); claims with any other top-up are skipped and counted. A supervisor can
  add prices on the Reference prices page. A change applies to files uploaded afterwards.
- **File layout** follows Petunjuk Teknis E-Klaim 5.1.1 (2017). Columns are matched by
  header name, but newer E-Klaim versions have not been tested. Encrypted TXT is not supported.
- **Special CMG codes**: RR04 follows the worked example in the Petunjuk Teknis. The
  Special Drug code (DD01) and the INA-CBG tariffs in the sample are illustrative.
- **Repeat top-ups** are counted within one file, not across batches.
- **Hospital volume** is a raw count, not scaled to hospital size.

A flag is a request for a human verifikator to look at a claim. It is not a fraud finding.
