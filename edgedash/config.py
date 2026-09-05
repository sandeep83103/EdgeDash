"""Load and validate project configuration from config.yaml at the repo root.

Environment variables are loaded here — the single place where .env is read
(steering rule 4). Every module that needs an env var reads it via os.environ
after this module has been imported.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "PyYAML is required: pip install pyyaml"
    ) from exc

from dotenv import load_dotenv

# Resolved once at import time so every caller gets the same root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "config.yaml"
_DOTENV_PATH = _REPO_ROOT / ".env"

# Load .env into os.environ once, at import time. override=False means
# variables already set in the shell take precedence over the file.
load_dotenv(_DOTENV_PATH, override=False)

_DEFAULTS: dict = {
    "target_role": "Software Engineer",
    "target_city": "Remote",
    "keywords": [],
    "my_skills": [],
    "experience_years": 0,
    "db_path": "edgedash.db",
    "min_fit_score": 50,
    "sources": ["arbeitnow"],
    "use_mock_fetcher": False,
    "llm_provider": "gemini",
    "llm_model": "gemini-3.6-flash",
    "scoring_batch_size": 25,
    # Seniority bands (ordered): junior < mid < senior < lead
    "target_seniority": "senior",
    # Scoring weights — must sum to 1.0
    "weight_skill_match":   0.45,
    "weight_seniority_fit": 0.25,
    "weight_location_fit":  0.15,
    "weight_recency":       0.15,
    "skill_aliases": {},
    # ── Planning thresholds & per-agent stop conditions ──────────────────────
    "fetch_interval_hours": 6,      # refetch only after this many hours
    "fetch_max_pages":      5,      # fetch stop condition
    "fetch_max_listings":   200,    # fetch stop condition
    "score_max_seconds":    120,    # score stop condition
    "analyse_max_seconds":  60,     # analyse stop condition
    # ── Verification thresholds (rule 39: each names the failure it catches) ──
    "min_score_spread":         10,    # catches score inflation: all scores bunched together
    "min_score_stdev":          5,     # catches score inflation: no real spread in the distribution
    "max_empty_extraction_pct": 20,    # catches a broken extractor: too many listings with no skills
    "max_skills_per_listing":   20,    # catches a model dumping a whole sentence as "skills"
    "min_gap_sample":           3,     # catches ranking a rumour: top gap from too few listings
    "max_data_age_days":        3,     # catches stale data: newest listing too old
    # Global daily cap on natural-language questions (public ask box). Protects
    # the free tier: a traffic spike can never exhaust it. When hit, the ask
    # box disables for the rest of the UTC day; the dashboard stays up.
    "daily_question_cap":       200,
    # Contrast-stretch gain applied only on a verification RETRY when the
    # score-spread check failed. >1 pushes scores away from the batch mean to
    # widen range + stdev. Deliberately moderate: it rescues a mildly-bunched
    # batch but will NOT manufacture spread from near-identical scores — a
    # genuinely flat batch stays flat and the cycle goes degraded (rule 38),
    # which is correct. Cranking this to force a pass would just be inflation.
    "score_spread_gain":        2.5,
}


@dataclass
class Config:
    target_role: str
    target_city: str
    keywords: list[str]
    my_skills: list[str]
    experience_years: int
    db_path: str
    min_fit_score: int
    sources: list[str]
    use_mock_fetcher: bool
    llm_provider: str
    llm_model: str
    scoring_batch_size: int
    target_seniority: str
    weight_skill_match: float
    weight_seniority_fit: float
    weight_location_fit: float
    weight_recency: float
    skill_aliases: dict[str, str]
    fetch_interval_hours: int
    fetch_max_pages: int
    fetch_max_listings: int
    score_max_seconds: int
    analyse_max_seconds: int
    min_score_spread: int
    min_score_stdev: float
    max_empty_extraction_pct: float
    max_skills_per_listing: int
    min_gap_sample: int
    max_data_age_days: int
    daily_question_cap: int
    score_spread_gain: float

    @property
    def abs_db_path(self) -> Path:
        """Resolve db_path relative to the repo root."""
        p = Path(self.db_path)
        return p if p.is_absolute() else _REPO_ROOT / p


def load_config(path: Path | None = None) -> Config:
    """Read config.yaml and return a validated Config instance.

    Raises FileNotFoundError if the file is absent — we never silently
    fall back to defaults when the user's own file is missing.
    """
    config_path = path or _CONFIG_PATH

    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at '{config_path}'.\n"
            "Copy config.yaml from the repo root and fill in your profile."
        )

    with config_path.open("r", encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh) or {}

    merged = {**_DEFAULTS, **raw}

    _validate(merged, config_path)

    return Config(
        target_role=str(merged["target_role"]),
        target_city=str(merged["target_city"]),
        keywords=_as_str_list(merged["keywords"], "keywords"),
        my_skills=_as_str_list(merged["my_skills"], "my_skills"),
        experience_years=_as_int(merged["experience_years"], "experience_years"),
        db_path=str(merged["db_path"]),
        min_fit_score=_as_int(merged["min_fit_score"], "min_fit_score"),
        sources=_as_str_list(merged["sources"], "sources"),
        use_mock_fetcher=bool(merged["use_mock_fetcher"]),
        llm_provider=str(merged["llm_provider"]),
        llm_model=str(merged["llm_model"]),
        scoring_batch_size=_as_int(merged["scoring_batch_size"], "scoring_batch_size"),
        target_seniority=str(merged["target_seniority"]),
        weight_skill_match=float(merged["weight_skill_match"]),
        weight_seniority_fit=float(merged["weight_seniority_fit"]),
        weight_location_fit=float(merged["weight_location_fit"]),
        weight_recency=float(merged["weight_recency"]),
        skill_aliases=_as_str_str_dict(merged["skill_aliases"], "skill_aliases"),
        fetch_interval_hours=_as_int(merged["fetch_interval_hours"], "fetch_interval_hours"),
        fetch_max_pages=_as_int(merged["fetch_max_pages"], "fetch_max_pages"),
        fetch_max_listings=_as_int(merged["fetch_max_listings"], "fetch_max_listings"),
        score_max_seconds=_as_int(merged["score_max_seconds"], "score_max_seconds"),
        analyse_max_seconds=_as_int(merged["analyse_max_seconds"], "analyse_max_seconds"),
        min_score_spread=_as_int(merged["min_score_spread"], "min_score_spread"),
        min_score_stdev=float(merged["min_score_stdev"]),
        max_empty_extraction_pct=float(merged["max_empty_extraction_pct"]),
        max_skills_per_listing=_as_int(merged["max_skills_per_listing"], "max_skills_per_listing"),
        min_gap_sample=_as_int(merged["min_gap_sample"], "min_gap_sample"),
        max_data_age_days=_as_int(merged["max_data_age_days"], "max_data_age_days"),
        daily_question_cap=_as_int(merged["daily_question_cap"], "daily_question_cap"),
        score_spread_gain=float(merged["score_spread_gain"]),
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _validate(data: dict, path: Path) -> None:
    required = ["target_role", "target_city"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise ValueError(
            f"config.yaml at '{path}' is missing required fields: {missing}"
        )


def _as_str_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise TypeError(f"config field '{field_name}' must be a YAML list.")
    return [str(item) for item in value]


def _as_int(value: object, field_name: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise TypeError(
            f"config field '{field_name}' must be an integer, got {value!r}."
        )


def _as_str_str_dict(value: object, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError(f"config field '{field_name}' must be a YAML mapping.")
    return {str(k).lower(): str(v).lower() for k, v in value.items()}
