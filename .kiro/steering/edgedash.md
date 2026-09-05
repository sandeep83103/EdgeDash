---
inclusion: always
---

# EdgeDash — Project Steering Rules

## What This Project Is

EdgeDash is an autonomous AI career intelligence agent. A scheduled loop that:

1. Fetches live job listings from configured sources
2. Scores each listing for fit against a user profile
3. Surfaces skill gaps between the profile and the market
4. Verifies its own output for quality and consistency
5. Publishes results to a read-only Streamlit dashboard

---

## Architecture

```
Trigger (scheduled)
  └─> Orchestrator
        ├─> Fetcher        (fetches raw job listings)
        ├─> Scorer         (scores listings against profile)
        └─> GapAnalyzer    (identifies skill gaps)
              └─> Verifier
                    └─> Storage
                          └─> Dashboard (read-only)
```

**Orchestrator contract:** reads state and delegates to sub-agents. It never
fetches job data or scores listings directly.

**Sub-agent contract:** each sub-agent has exactly one goal and one stop
condition. Do not expand a sub-agent's responsibility without flagging it.

Do not deviate from this architecture without explaining the reason and getting
explicit confirmation first.

---

## Hard Rules

### 1 — Python version and dependencies

- Python 3.11+ only.
- Prefer the standard library. Add a third-party dependency only when it
  genuinely saves real work (not just convenience). Before adding any new
  package, state what it is, why the stdlib is insufficient, and what it
  replaces.

### 2 — Storage access is centralised

- All database reads and writes go through a single `storage` module that
  exposes a thin interface.
- No other module may `import sqlite3` directly.
- The backend will migrate from SQLite to hosted Postgres in week 4. That
  change must be achievable by editing one file only.

### 3 — No hardcoded user data

- Role titles, cities, keywords, skills, and any other user-specific values
  must live in configuration (e.g. `config.yaml` or equivalent).
- The code must work unchanged for a different user who only edits config.

### 4 — No secrets in code

- API keys, database URLs, and credentials are environment variables only.
- They are loaded in exactly one place (e.g. `settings.py` or `config.py`).
- Never echo or log secret values.

### 5 — Cycle logging is mandatory

Every agent run must write a row to the `cycle_log` table containing at minimum:

| column          | description                          |
|-----------------|--------------------------------------|
| `agent_name`    | which agent ran                      |
| `started_at`    | UTC timestamp                        |
| `records_touched` | count of records created/updated   |
| `status`        | `pass` or `fail`                     |
| `retry_reason`  | populated on failure/retry, else null|

### 6 — Fail loudly

- No bare `except: pass` or silent swallowing of exceptions.
- If something is wrong it must surface — raise, log at ERROR level, or both.
- Catching a specific exception is fine; catching everything and doing nothing
  is not.

### 7 — Type hints and docstrings

- Every function signature must have type hints on all parameters and the
  return type.
- Add a docstring only when the intent is not obvious from the function name
  and its signature. Do not add boilerplate docstrings that restate the name.

### 8 — File length

- Keep files under ~150 lines. If a file is approaching that limit, split it
  before it becomes a problem. Flag this proactively rather than waiting to be
  asked.

---

## Style Guidelines

- Small, single-purpose, testable functions.
- Plain, readable Python over clever Python. Prioritise clarity.
- When asked to build one module, build that module only. Do not scaffold the
  whole application unless explicitly asked.
- Imports ordered: stdlib → third-party → local, separated by blank lines.

---

## Network & Sources

### 9 — Sources are plug-in classes; the Fetcher is source-agnostic

Every external job source lives behind a `Source` class with a uniform
interface. The Fetcher iterates sources and collects results — it contains no
source-specific parsing logic. Adding a new source means adding a new `Source`
class only; the Fetcher must never need to be edited.

### 10 — Every Source returns a normalised record shape

A `Source` returns a `list[dict]`. Each dict must contain exactly these keys:

| key           | type / note                              |
|---------------|------------------------------------------|
| `source`      | str — short identifier, e.g. `"adzuna"`  |
| `external_id` | str — the source's own ID for the job    |
| `title`       | str                                      |
| `company`     | str                                      |
| `location`    | str \| None                              |
| `url`         | str                                      |
| `description` | str \| None                              |
| `posted_at`   | ISO-8601 str \| None                     |
| `raw`         | dict — the unmodified API/scrape payload |

Missing values are `None`. Never use empty string or `"N/A"` as a sentinel.

### 11 — All network calls go through one shared helper

A single `http_get(url, ...)` helper (or equivalent) handles timeouts (10 s
default), retry logic (2 attempts, exponential backoff), and the `User-Agent`
header. No bare `requests.get()` or `urllib.request.urlopen()` anywhere else
in the codebase.

### 12 — A source failure must never kill the cycle

Wrap each source call individually. On failure: catch the exception, log it to
`cycle_log` with `status = "fail"` and a descriptive notes field, then
continue to the next source. One dead job board must not prevent the other
sources from running.

### 13 — Source credentials come from environment variables via .env

API keys and tokens for each source are loaded from environment variables.
They may be set in a `.env` file at the repo root, which is gitignored.
Never put a literal key in source code. Never put a key in `config.yaml`.
If a required key is absent, that source logs a clear `"missing API key —
skipping"` message and skips itself without raising or stopping the cycle.

### 14 — Be a respectful client

- Rate-limit each source to at most 1 request per second.
- Always send a descriptive `User-Agent` header identifying the client.
- Honour any page-size or rate limits documented by the source.
- Do not hammer an endpoint on retry — use backoff.

---

## Intelligence & Scoring

### 15 — All LLM calls go through one module

`edgedash/llm.py` is the only file that imports an LLM SDK. It exposes one
public function. The provider name and model name come from `config` — never
hardcoded. Default rate limit: 1 request per second, capped at 15 per minute,
to stay within a free tier. No other module may import an LLM library directly.

### 16 — The model extracts facts; Python does the arithmetic

Never ask a model for a score, ranking, or numeric rating. The model's job is
to extract structured facts from a job description (e.g. required skills,
seniority, location type). All scoring arithmetic — weights, totals, thresholds
— lives in one deterministic Python function. The model never sees the scoring
weights or the final score.

### 17 — Every model response is validated before use

Validate every response against an explicit schema before touching its contents.
A response that fails validation is retried once, then logged as a failure for
that listing only. A validation failure must never crash the cycle or skip the
remaining listings. Never call `json.loads` on raw model text without a
validation and repair path that handles malformed output.

### 18 — Scoring is idempotent; extraction results are cached

Never re-score a listing that already has a `fit_score`. The Scorer selects
only `WHERE fit_score IS NULL`. Cache extraction results keyed on a hash of the
job description text — the same description must never be sent to the model
twice, even across separate cycle runs.

### 19 — Score reasons are generated by code, not by the model

Every scored listing carries a human-readable `fit_reason`. That reason is
assembled by Python from the score components (e.g. which skills matched, which
were missing, what the seniority penalty was). The model never writes free-text
explanations that end up stored or displayed.

### 20 — Score distribution is logged every run; suspect runs are flagged

After each scoring run, log the distribution (count, min, max, mean, spread)
to `cycle_log`. A run where all scores fall within a 10-point band is a suspect
run — log it explicitly as such so it can be investigated. A healthy scoring
run should produce meaningful spread across the range.

### 21 — Batch size is capped per cycle

A `scoring_batch_size` config field (default `25`) limits how many listings are
scored in a single cycle. This makes cost and rate-limit blowups structurally
impossible — even if thousands of unscored listings accumulate, the Scorer
processes at most `scoring_batch_size` per run.

---

## Aggregate Analysis

### 22 — Aggregates are deterministic SQL and Python

No LLM call may produce, adjust, or rank an aggregate number. A model may
only suggest canonical groupings for a human to approve — it never writes
numbers that end up in a report. All gap counts, rankings, and trend figures
are computed by SQL queries or pure Python functions.

### 23 — Skill names are canonicalised through a config-owned alias map

Skill name normalisation goes through an explicit alias map defined in
`config.yaml`. The map is human-readable and human-maintained. Never
auto-merge skill names by model judgement or string-similarity heuristic
alone — silent merges produce invisible errors that are hard to audit.

### 24 — Gap ranking is weighted by listing fit score

A skill gap found in a listing scored 20 counts far less than the same gap
in a listing scored 85. Gaps are ranked by score-weighted frequency, not raw
frequency. Never present a raw-frequency ranking as the primary sort order.

### 25 — Every gap report run writes a timestamped snapshot

Gap reports are append-only. Never overwrite the previous report. Each run
writes a new row or file carrying a UTC timestamp. Trend over time — whether
a skill gap is growing or shrinking across runs — is a first-class output,
not something reconstructed later.

### 26 — Every aggregate number is traceable to its source rows

Any reported gap must be able to list the specific listing IDs it was
computed from. No number appears in the dashboard that cannot be drilled
into. The storage schema must preserve the link between each gap entry and
the listings that contributed to it.

### 27 — Sample size is reported alongside every aggregate

A gap computed from 3 listings and one computed from 90 listings must never
be presented as equally reliable. Every aggregate in the dashboard and every
row in the gap report carries the count of listings it was derived from.
Reliability context is not optional.

---

## Orchestration

### 28 — The Orchestrator decides; it never runs a fixed sequence

The Orchestrator reads system state and chooses which agents to run based on
that state. It does not execute a hardcoded pipeline. Skipping an agent
because there is no work for it (e.g. no unscored listings) is a SUCCESSFUL
outcome, not a failure — it must never be logged or reported as an error.

### 29 — Every delegation carries an explicit goal and stop condition

When the Orchestrator delegates to a sub-agent it passes an explicit goal and
explicit limits: max items to process, max duration, or both. A sub-agent
never decides its own limits — the Orchestrator sets them so cost and runtime
stay bounded and predictable.

### 30 — The Orchestrator never does an agent's work

The Orchestrator reads state, delegates, collects results, and logs. It
contains no fetching, scoring, or analysis logic. If a piece of domain logic
is creeping into the Orchestrator, it belongs in an agent instead.

### 31 — The plan is printed and logged before execution

Before running anything, the Orchestrator prints and logs its plan: which
agents will run, which are skipped, and the specific state value that drove
each decision. The plan is auditable after the fact, not just visible in the
moment.

### 32 — One sub-agent failure does not stop the cycle

If a sub-agent fails, log the failure, continue with the remaining planned
agents, and mark the cycle as partial. A single failing agent must never
abort the whole cycle or prevent independent agents from running.

### 33 — Every cycle writes exactly one summary row

Each cycle writes one summary record capturing: what ran, what was skipped
and why, the duration per agent, and the overall outcome (pass / partial /
fail). This is the single source of truth for what happened during a cycle.

---

## Verification

### 34 — The Verifier judges; it never repairs

The Verifier assesses output plausibility and returns a verdict plus a reason.
It never repairs, rewrites, adjusts, or deletes data. Deciding what to do about
a failed verdict is the Orchestrator's job, not the Verifier's. The Verifier is
read-only with respect to the data it inspects.

### 35 — Verification checks plausibility, not correctness

There is no ground truth for a fit score, so verification never asserts that a
value is "right". It asserts properties of the output's distribution and shape —
spread, range, null rate, count — never the accuracy of any single value. A
check answers "does this output look plausible?", never "is this output correct?".

### 36 — A failed verification triggers at most one retry, then stops

On a failed verdict the Orchestrator may retry the failing agent exactly once,
with adjusted context. If verification fails again, the cycle is marked
"degraded" and stops. Never retry in an unbounded loop — one retry is the hard
ceiling.

### 37 — Every verdict is logged with the check and the observed value

A verdict written to `cycle_log` names the specific check that failed and the
observed value that failed it — e.g. "spread=3 below min_spread=10", never just
"failed". A verdict must be diagnosable from the log alone.

### 38 — Only verified cycles are readable; stale-good beats fresh-unverified

The dashboard reads only cycles with a passing verdict. A failed cycle must
never overwrite the last known-good data. Stale verified data always beats
fresh unverified data — a degraded cycle leaves the previous good output in
place rather than publishing something implausible.

### 39 — Verification thresholds live in config, with intent comments

Every verification threshold lives in `config.yaml`, never hardcoded. Each
threshold carries a comment stating the specific failure it is designed to
catch — so the reader knows not just the number but why it exists and what
tripping it would mean.

---

## Natural Language Queries

### 40 — Never generate SQL from a model

No text-to-SQL, ever, in any form. The model selects from a fixed registry of
parameterised query functions that we wrote by hand. It never composes,
completes, edits, or emits a query — not even a fragment. The registry is the
only way a question reaches the database.

### 41 — Query tools are read-only, parameterised, and validated

Every query tool is read-only, uses parameterised statements, and takes typed
parameters that are validated and clamped to a safe range before execution. A
model-supplied parameter is untrusted input — coerce it to the declared type,
reject what will not coerce, and clamp numeric ranges (e.g. a limit) to sane
bounds. Never interpolate a model value into a query string.

### 42 — The model appears exactly twice per question

Once to ROUTE (pick a tool from the registry and its parameters) and once to
PHRASE (turn the returned rows into prose). It never touches the database in
either call. Between the two calls, our deterministic code runs the chosen
tool. Route → run → phrase; the model is bookends, never the engine.

### 43 — Phrasing uses only the numbers in the rows

The phrasing call may use ONLY the values present in the rows it was handed. It
must not estimate, extrapolate, add outside context, or infer a value that is
not in the data. If the rows are empty, it must say so plainly rather than
inventing an answer. No figure may appear in prose that is not present in the
returned rows.

### 44 — Every answer shows its underlying rows

No prose answer appears without the data that produced it. The rows the tool
returned are displayed alongside the phrased answer, so the reader can check
the prose against the source every time. An answer without its data is not an
acceptable answer.

### 45 — No match means say so, never guess

If no tool in the registry matches the question, say so and list what CAN be
asked. Never guess at the closest tool, never bend the question to fit a tool,
and never answer from the model's general knowledge. "I can't answer that;
here is what I can answer" is the correct response.

### 46 — Query tools read the last passing cycle only

Every query tool reads from the last passing cycle, per rule 38. Questions are
answered from verified data only — a failed or degraded cycle never becomes the
basis for an answer.

---

## Deployment

### 47 — Never rely on the local filesystem for durable state

Hosting filesystems are ephemeral — a restart wipes them. Nothing that must
survive a restart may live on local disk. All persistent state lives in the
hosted database. A local SQLite file is acceptable only for offline
development, never as the source of truth in deployment.

### 48 — Secrets come from the environment, read in one place

Every secret is read from an environment variable in a single place. No secret
is ever committed to the repo, printed, logged, or exposed in an error message
or traceback. When an error touches a connection string or key, redact it
before it can surface.

### 49 — Scheduler and dashboard are separate processes

The scheduled job and the dashboard run as separate processes that share only
the database. The dashboard never runs a cycle; the scheduler never serves a
page. Neither imports the other's entry point. Their only contract is the
data in the shared store.

### 50 — The app starts and renders on a broken database

The deployed app must start and render even when the database is empty,
unreachable, or mid-migration. In every one of those cases it shows a clear
status message, not a stack trace. A stranger must never see a traceback —
degrade to a plain "data not available yet" state instead.

### 51 — The scheduled job is idempotent and bounded

The scheduled job is safe to run twice: running it again must not corrupt or
double-count anything. It has a hard timeout and stays inside free-tier limits
on requests, compute, and time. A run that overruns is stopped, not left to
consume the budget.

---

## Scope Discipline

If a request would require deviating from the architecture, breaking a hard
rule, or adding a significant dependency, pause and explain the conflict before
writing any code. Propose the minimum change needed, then wait for confirmation.
