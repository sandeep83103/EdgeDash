"""Read and write the user-editable fields of config.yaml in place.

This is the ONE module that writes config.yaml. It exists so the dashboard can
let a user edit their own profile (target role, city, keywords, skills) without
hand-editing YAML, while keeping config.yaml the single source of truth
(steering rule 3 — no hardcoded user data; it all still lives in config).

Only four user-profile fields are writable here:
    target_role   (scalar str)
    target_city   (scalar str)
    keywords      (list[str])
    my_skills     (list[str])

Comments and every other field are preserved: we rewrite only the specific
lines that own these fields rather than re-serialising the whole document
(PyYAML would drop all comments). No new dependency is needed.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise ImportError("PyYAML is required: pip install pyyaml") from exc

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "config.yaml"

# Fields this editor is allowed to touch. Anything else is off-limits.
_SCALAR_FIELDS = ("target_role", "target_city")
_LIST_FIELDS = ("keywords", "my_skills")


class ProfileValues:
    """The user-profile subset of config that the dashboard can edit."""

    def __init__(
        self,
        target_role: str,
        target_city: str,
        keywords: list[str],
        my_skills: list[str],
    ) -> None:
        self.target_role = target_role
        self.target_city = target_city
        self.keywords = keywords
        self.my_skills = my_skills


def read_profile(path: Path | None = None) -> ProfileValues:
    """Read the four editable profile fields from config.yaml."""
    config_path = path or _CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"config.yaml not found at '{config_path}'.")

    with config_path.open("r", encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh) or {}

    return ProfileValues(
        target_role=str(raw.get("target_role", "")),
        target_city=str(raw.get("target_city", "")),
        keywords=[str(k) for k in (raw.get("keywords") or [])],
        my_skills=[str(s) for s in (raw.get("my_skills") or [])],
    )


def write_profile(values: ProfileValues, path: Path | None = None) -> None:
    """Update the four editable fields in config.yaml in place.

    Comments and all other fields are preserved. Raises ValueError if a
    required scalar is blank so we never write an invalid profile.
    """
    if not values.target_role.strip():
        raise ValueError("target_role must not be empty.")
    if not values.target_city.strip():
        raise ValueError("target_city must not be empty.")

    config_path = path or _CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"config.yaml not found at '{config_path}'.")

    lines = config_path.read_text(encoding="utf-8").splitlines()

    lines = _replace_scalar(lines, "target_role", values.target_role)
    lines = _replace_scalar(lines, "target_city", values.target_city)
    lines = _replace_list(lines, "keywords", values.keywords)
    lines = _replace_list(lines, "my_skills", values.my_skills)

    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Internal line-level rewriters ─────────────────────────────────────────────

def _replace_scalar(lines: list[str], key: str, value: str) -> list[str]:
    """Replace `key: ...` on its own top-level line, keeping any trailing comment."""
    pattern = re.compile(rf"^{re.escape(key)}\s*:")
    out: list[str] = []
    replaced = False
    for line in lines:
        if not replaced and pattern.match(line):
            out.append(f'{key}: "{_escape(value)}"')
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f'{key}: "{_escape(value)}"')
    return out


def _replace_list(lines: list[str], key: str, items: list[str]) -> list[str]:
    """Replace a top-level `key:` block and its `  - item` entries.

    Preserves any comment lines that appear between the key and its first
    list item (e.g. the guidance comments above `keywords`).
    """
    key_pattern = re.compile(rf"^{re.escape(key)}\s*:\s*$")
    item_pattern = re.compile(r"^\s+-\s+")
    comment_pattern = re.compile(r"^\s+#")

    out: list[str] = []
    i = 0
    replaced = False
    n = len(lines)

    while i < n:
        line = lines[i]
        if not replaced and key_pattern.match(line):
            out.append(f"{key}:")
            i += 1
            # Preserve leading comment lines that document the block.
            while i < n and comment_pattern.match(lines[i]):
                out.append(lines[i])
                i += 1
            # Skip the existing list items — they are being replaced.
            while i < n and (item_pattern.match(lines[i]) or lines[i].strip() == ""):
                if lines[i].strip() == "":
                    break
                i += 1
            for item in items:
                out.append(f"  - {_escape_item(item)}")
            replaced = True
        else:
            out.append(line)
            i += 1

    if not replaced:
        out.append(f"{key}:")
        for item in items:
            out.append(f"  - {_escape_item(item)}")
    return out


def _escape(value: str) -> str:
    """Escape a scalar for a double-quoted YAML string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _escape_item(value: str) -> str:
    """Quote a list item only when it contains YAML-significant characters."""
    stripped = value.strip()
    if stripped and re.search(r'[:#\'"\[\]{},&*!|>%@`]', stripped):
        return f'"{_escape(stripped)}"'
    return stripped
