# EdgeDash

EdgeDash is an autonomous career intelligence agent that runs on a schedule,
fetches live job listings from configured sources, scores each listing for fit
against your skills profile, identifies the skill gaps the market is signalling,
verifies its own output for consistency, and publishes the results to a
read-only Streamlit dashboard — giving you a daily, evidence-based picture of
where you stand in your target job market.

---

## Architecture

```
Trigger (scheduled)
    |
    v
Orchestrator
    |-- reads state, builds plan, delegates -- never fetches or scores directly
    |
    +----------+----------+
    |          |          |
    v          v          v
 Fetcher   Scorer   GapAnalyzer
    |          |          |
    +----------+----------+
               |
               v
           Verifier
               |
               v
           Storage  <-- single module, sole owner of the database
               |
               v
         Dashboard (read-only)
```

---

## Current status

### Built
- [x] `edgedash/config.py` — `Config` dataclass loaded from `config.yaml`
- [x] `edgedash/storage.py` — isolated storage module (sole owner of the DB)
- [x] `edgedash/agents/` — `Fetcher`, `Scorer`, `GapAnalyzer`, `Verifier`
      (plus `MockFetcher` for offline development)
- [x] `edgedash/sources/` — plug-in job sources: Arbeitnow, Apify (Indeed),
      and Naukri.com, all behind a uniform `Source` interface
- [x] `edgedash/llm.py` — single gateway for all LLM calls
- [x] `edgedash/orchestrator.py` — `run_cycle`: reads state, builds plan,
      dispatches agents, verifies output, logs every run to `cycle_log`
- [x] `app.py` — Streamlit dashboard (profile editor, sources, test run,
      schedule, clickable listings, ask-your-data)
- [x] `.github/workflows/cycle.yml` — scheduled trigger (GitHub Actions cron)
- [x] `run_cycle.py` — entry point

### Backlog
- [ ] Migrate Storage backend from SQLite to hosted Postgres (one-file change)

---

## Setup

**Requirements:** Python 3.11 or later.

```
pip install -r requirements.txt
```

**Configure your profile**

Edit `config.yaml` at the repo root (or use the dashboard's profile editor,
which writes back to the same file):

```yaml
target_role: "Data Analyst"
target_city: "Bengaluru"

keywords:
  - SQL
  - Python
  - Power BI

my_skills:
  - SQL
  - Python
  - pandas

experience_years: 3
min_fit_score: 50
db_path: "edgedash.db"
```

All user-specific values live here. No role, city, skill, or keyword is
hardcoded anywhere in the source.

**Configure secrets**

Copy `.env.example` to `.env` and fill in the values you need. Secrets
(API keys, database URLs) live in environment variables only — never in
`config.yaml` and never committed:

- `GEMINI_API_KEY` — required when `llm_provider` is `gemini` (the default)
- `APIFY_TOKEN` — required for the `apify` source; also used by `naukri`
- `NAUKRI_APIFY_ACTOR` — optional override for the Naukri Apify actor

**Run a cycle**

```
python run_cycle.py
```

The first run initialises the database, fetches listings, scores them,
analyses gaps, verifies the output, and prints a cycle summary.

---

## Dashboard

```
streamlit run app.py
```

The dashboard reads through the storage module only — it never runs the
scheduled cycle in its own process (the GitHub Actions scheduler does that).
It offers:

- **Search profile editor** — edit `target_role`, `target_city`, keywords,
  and your skills; saves straight back to `config.yaml`.
- **Sources searched** — shows which job portals the current cycle draws from.
- **Test run** — launches one cycle as a separate subprocess for a manual
  check, with a live percentage progress bar. This is not the scheduler.
- **Automatic schedule** — pick the daily IST time the scheduler should fire;
  the dashboard writes the matching UTC cron into the workflow (commit and
  push for it to take effect).
- **Top scored listings** — each row has an **Apply** link that opens the
  posting on its source so you can apply directly.
- **Top skill gaps** and an **Ask your data** natural-language box, both
  answered only from the last verified cycle.

---

## Sources

Every job board is a plug-in `Source` class behind a uniform interface; the
Fetcher iterates them and contains no board-specific logic. Enable or disable
sources in the `sources:` list in `config.yaml`.

| Source      | Access                                            | Key needed        |
|-------------|---------------------------------------------------|-------------------|
| `arbeitnow` | Free public job-board API                         | none              |
| `apify`     | Indeed via an Apify actor                         | `APIFY_TOKEN`     |
| `naukri`    | Naukri.com via an Apify actor (best-effort direct fallback) | `APIFY_TOKEN` (recommended) |

A source that fails or is missing its key logs the reason and skips itself —
one dead board never stops the cycle.

---

## Design decisions

**Storage is isolated behind one module.**
`edgedash/storage.py` is the only file that imports `sqlite3`. Everything
else calls its thin interface. When the backend moves to Postgres in week 4,
only `storage.py` needs to change — connection logic, DDL, and SQL dialect
are all in one place, and no other module is affected.

**Listing IDs are stable hashes of source and URL.**
`make_listing_id(source, url)` produces the same 24-character hex ID every
time for the same job posting, regardless of when it was fetched. Combined
with `INSERT OR IGNORE`, this means the same job can never be double-counted
across runs, and the count returned by `upsert_listings` is always an honest
measure of genuinely new listings.

**The Orchestrator delegates; it never does the work itself.**
Keeping fetch, score, and analysis logic out of the Orchestrator means each
concern can be developed, tested, and replaced independently. The Orchestrator's
only job is to read state, decide what needs to run, and hand off to the right
agent — making the control flow easy to follow and the registry swap (mock
to real) a one-line change.
