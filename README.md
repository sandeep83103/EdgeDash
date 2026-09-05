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
- [x] `edgedash/storage.py` — isolated storage module (SQLite, three tables)
- [x] `edgedash/agents/base.py` — `Agent` protocol and `AgentResult` dataclass
- [x] `edgedash/agents/mock_fetcher.py` — **temporary** mock; returns 12 fake
      listings per run, 4 with stable IDs to prove deduplication
- [x] `edgedash/orchestrator.py` — `run_cycle`: reads state, builds plan,
      dispatches agents, logs every run to `cycle_log`
- [x] `run_cycle.py` — entry point

### Week 2
- [ ] Real `Fetcher` — live listings via job-board APIs or scraping
- [ ] `Scorer` — fit scoring against your profile
- [ ] Remove `MockFetcher`

### Week 3
- [ ] `GapAnalyzer` — surfaces skills you are missing vs. the market
- [ ] `Verifier` — checks output quality and flags anomalies
- [ ] Streamlit dashboard (read-only, pulls from Storage)

### Week 4
- [ ] Migrate Storage backend from SQLite to hosted Postgres (one-file change)
- [ ] Scheduled trigger (cron or cloud scheduler)

---

## Setup

**Requirements:** Python 3.11 or later.

```
pip install pyyaml==6.0.2
```

That is the only third-party dependency at this stage.

**Configure your profile**

Copy or edit `config.yaml` at the repo root:

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
hardcoded anywhere in the source. Secrets (API keys, database URLs) go in
environment variables, not in this file.

**Run a cycle**

```
python run_cycle.py
```

The first run initialises the database, fetches listings, and prints a
cycle summary. Run it again immediately to see deduplication in action —
the four stable mock listings will report "already in DB".

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
