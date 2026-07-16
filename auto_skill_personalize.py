"""Local, per-machine personalization.

A lightweight Beta-distribution bandit that nudges skill/tag ranking based
on this machine's own feedback history. Everything here is local-only: no
account, no network call, and no prompt text is ever stored. This module is
intentionally standalone (no import of auto_skill_core) so auto_skill_core
can import it without creating a cycle, mirroring how auto_skill_identity.py
and auto_skill_auth.py stay dependency-free.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_MIN_OBSERVATIONS = 5
_BANDIT_BLEND = 0.3
_MAX_ROUTE_HISTORY = 200
_SUCCESS_OUTCOMES = {"used", "installed"}
_FAILURE_OUTCOMES = {"skipped", "failed", "dismissed"}


def personalization_enabled() -> bool:
    return os.getenv("AUTOSKILL_PERSONALIZATION", "1").strip().lower() in _TRUTHY_VALUES


def get_weights_path() -> Path:
    override = os.getenv("AUTOSKILL_WEIGHTS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".autoskill" / "weights.json"


def get_route_history_path() -> Path:
    override = os.getenv("AUTOSKILL_ROUTE_HISTORY_PATH")
    if override:
        return Path(override)
    return Path.home() / ".autoskill" / "route_history.json"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)  # best effort on Windows
        except OSError:
            pass
        temp.replace(path)
    except OSError:
        # A read-only profile must never block routing; the caller simply
        # loses this update instead of crashing.
        pass


def _slug_key(value: str) -> str:
    return (value or "").strip().lower()


def _arm(weights: dict[str, Any], key: str) -> dict[str, float]:
    arms = weights.setdefault("arms", {})
    arm = arms.get(key)
    if not isinstance(arm, dict):
        arm = {}
        arms[key] = arm
    arm.setdefault("alpha", 1.0)
    arm.setdefault("beta", 1.0)
    return arm


def record_route(route_id: str, skill_id: str, tags: list[str] | None, platform: str = "") -> None:
    """Best-effort local bookkeeping so a later outcome can be mapped back to
    the skill it was about (record_route_feedback only carries a route_id).
    Never stores prompt text."""
    if not personalization_enabled() or not route_id or not skill_id:
        return
    path = get_route_history_path()
    data = _load_json(path)
    entries = data.get("entries")
    if not isinstance(entries, list):
        entries = []
    entries.append(
        {
            "route_id": route_id,
            "skill_id": _slug_key(skill_id),
            "tags": sorted({_slug_key(t) for t in (tags or []) if t}),
            "platform": _slug_key(platform),
            "ts": time.time(),
        }
    )
    data["entries"] = entries[-_MAX_ROUTE_HISTORY:]
    _atomic_write(path, data)


def _find_route(route_id: str) -> dict[str, Any] | None:
    data = _load_json(get_route_history_path())
    for entry in reversed(data.get("entries") or []):
        if isinstance(entry, dict) and entry.get("route_id") == route_id:
            return entry
    return None


def record_outcome(route_id: str, outcome: str) -> None:
    """Best-effort local bandit update from a route outcome. Never raises."""
    if not personalization_enabled():
        return
    outcome = (outcome or "").strip().lower()
    if outcome not in _SUCCESS_OUTCOMES and outcome not in _FAILURE_OUTCOMES:
        return
    entry = _find_route((route_id or "").strip())
    if entry is None:
        return
    success = outcome in _SUCCESS_OUTCOMES
    weights_path = get_weights_path()
    weights = _load_json(weights_path)
    keys = [f"skill:{entry['skill_id']}"] + [f"tag:{tag}" for tag in entry.get("tags") or []]
    for key in keys:
        arm = _arm(weights, key)
        if success:
            arm["alpha"] += 1.0
        else:
            arm["beta"] += 1.0
    _atomic_write(weights_path, weights)


def estimated_weight(skill_id: str, tags: list[str] | None = None) -> float:
    """Return a smoothed 0..1 estimate of how well this skill/its tags have
    performed on this machine. Neutral 0.5 until enough observations exist
    (cold-start guard, same idea as ms's cold_start_threshold)."""
    weights = _load_json(get_weights_path())
    arms = weights.get("arms") if isinstance(weights.get("arms"), dict) else {}

    skill_arm = arms.get(f"skill:{_slug_key(skill_id)}")
    if isinstance(skill_arm, dict):
        alpha = float(skill_arm.get("alpha", 1.0))
        beta = float(skill_arm.get("beta", 1.0))
        if alpha + beta - 2.0 >= _MIN_OBSERVATIONS:
            return alpha / (alpha + beta)

    tag_estimates: list[float] = []
    for tag in tags or []:
        tag_arm = arms.get(f"tag:{_slug_key(tag)}")
        if isinstance(tag_arm, dict):
            alpha = float(tag_arm.get("alpha", 1.0))
            beta = float(tag_arm.get("beta", 1.0))
            if alpha + beta - 2.0 >= _MIN_OBSERVATIONS:
                tag_estimates.append(alpha / (alpha + beta))
    if tag_estimates:
        return sum(tag_estimates) / len(tag_estimates)
    return 0.5


def _candidate_score(candidate: dict[str, Any]) -> float:
    for key in ("routing_score", "similarity"):
        value = candidate.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _candidate_identity(candidate: dict[str, Any]) -> tuple[str, list[str]]:
    skill_id = str(candidate.get("name") or candidate.get("url") or "")
    category = candidate.get("category")
    tags = [str(category)] if category else []
    return skill_id, tags


def apply_personalization(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reorder an already-gated candidate list using this machine's learned
    weights. Never touches routing_tier, risk_score, or verification --
    personalization only nudges ordering within what routing already
    decided was safe to show, it never changes which content is trusted."""
    if not personalization_enabled() or len(candidates) < 2:
        return candidates

    original_order = {id(candidate): index for index, candidate in enumerate(candidates)}

    def sort_key(candidate: dict[str, Any]) -> tuple[float, int]:
        skill_id, tags = _candidate_identity(candidate)
        weight = estimated_weight(skill_id, tags)
        nudged = _candidate_score(candidate) * (1.0 + _BANDIT_BLEND * (weight - 0.5))
        return (nudged, -original_order[id(candidate)])

    return sorted(candidates, key=sort_key, reverse=True)


def weights_summary() -> dict[str, Any]:
    weights = _load_json(get_weights_path())
    arms = weights.get("arms") if isinstance(weights.get("arms"), dict) else {}
    summary = []
    for key, arm in arms.items():
        if not isinstance(arm, dict):
            continue
        alpha = float(arm.get("alpha", 1.0))
        beta = float(arm.get("beta", 1.0))
        observations = max(0.0, alpha + beta - 2.0)
        summary.append(
            {
                "key": key,
                "estimated_weight": round(alpha / (alpha + beta), 4),
                "observations": int(observations),
                "learned": observations >= _MIN_OBSERVATIONS,
            }
        )
    summary.sort(key=lambda item: item["observations"], reverse=True)
    return {"arms": summary, "path": str(get_weights_path())}


def reset_weights() -> None:
    try:
        get_weights_path().unlink()
    except FileNotFoundError:
        pass
