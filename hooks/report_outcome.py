"""Claude Code Stop hook for Measurement Mode outcome reporting.

hooks/skill_suggest.py journals a route_id whenever the backend assigns a
Measurement Mode arm (routed or holdout) to a task -- see
_track_measurement_route there. This hook runs once per session, when Claude
Code fires the Stop event: it reads that journal, derives session-level
outcome numbers (turns, tool calls, elapsed time, and total tokens where the
transcript exposes usage) from the session transcript, and reports them
against every journaled route_id via POST /route-outcome-metrics. Without
this, an opted-in account's measured_lift never has any samples to compare --
see backend/local_store.py:measurement_mode_lift.

Fails open like hooks/skill_suggest.py: any error here is swallowed and never
blocks Claude Code from stopping. Never reads or sends prompt/response text,
only counts and timestamps already present in the local transcript.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

AUTOSKILL_URL = os.getenv("AUTOSKILL_URL", "https://skills.autoskill.dev").rstrip("/")
CLIENT_NAME = "auto-skill-hook"
CLIENT_VERSION = "0.1.0"
TIMEOUT_SECONDS = float(os.getenv("AUTOSKILL_HOOK_TIMEOUT_SECONDS", "1.0"))
SESSIONS_DIR = (
    Path(os.getenv("AUTOSKILL_SESSIONS_DIR"))
    if os.getenv("AUTOSKILL_SESSIONS_DIR")
    else Path.home() / ".autoskill" / "sessions"
)

_opener = urllib.request.build_opener()
_opener.addheaders = [("User-Agent", f"{CLIENT_NAME}/{CLIENT_VERSION}")]
urllib.request.install_opener(_opener)


def _auth_headers() -> dict[str, str]:
    override = os.getenv("AUTOSKILL_CREDENTIALS_PATH")
    path = Path(override) if override else Path.home() / ".autoskill" / "credentials.json"
    try:
        token = json.loads(path.read_text(encoding="utf-8")).get("token")
    except Exception:
        return {}
    return {"Authorization": f"Bearer {token}"} if token else {}


def _journaled_route_ids(session_id: str) -> list[str]:
    if not session_id:
        return []
    path = SESSIONS_DIR / f"{session_id}.jsonl"
    if not path.is_file():
        return []
    route_ids: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            route_id = str((record or {}).get("route_id") or "").strip()
            if route_id:
                route_ids.append(route_id)
    except OSError:
        return []
    return route_ids


def _parse_timestamp(value: str) -> float | None:
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _session_outcome_metrics(transcript_path: str) -> dict[str, int]:
    """Best-effort counts derived from the transcript's own structure --
    never its text. A malformed or unreadable transcript yields an empty
    metrics dict rather than raising."""
    metrics: dict[str, int] = {}
    if not transcript_path:
        return metrics
    path = Path(transcript_path)
    if not path.is_file():
        return metrics

    turns = 0
    tool_calls = 0
    total_tokens = 0
    have_tokens = False
    first_ts: float | None = None
    last_ts: float | None = None

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return metrics

    for line in lines:
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue

        ts = _parse_timestamp(str(entry.get("timestamp") or ""))
        if ts is not None:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)

        entry_type = entry.get("type")
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}

        if entry_type == "user" and "toolUseResult" not in entry:
            turns += 1

        if entry_type == "assistant":
            content = message.get("content")
            if isinstance(content, list):
                tool_calls += sum(1 for block in content if isinstance(block, dict) and block.get("type") == "tool_use")
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if isinstance(input_tokens, int) or isinstance(output_tokens, int):
                have_tokens = True
                total_tokens += int(input_tokens or 0) + int(output_tokens or 0)

    if turns:
        metrics["turns"] = turns
    if tool_calls:
        metrics["tool_calls"] = tool_calls
    if have_tokens:
        metrics["total_tokens"] = total_tokens
    if first_ts is not None and last_ts is not None and last_ts > first_ts:
        metrics["elapsed_seconds"] = int(last_ts - first_ts)
    return metrics


def _report(route_id: str, metrics: dict[str, int]) -> None:
    if not route_id or not AUTOSKILL_URL:
        return
    try:
        body = json.dumps({"route_id": route_id, **metrics}).encode("utf-8")
        request = urllib.request.Request(
            f"{AUTOSKILL_URL}/route-outcome-metrics",
            data=body,
            headers={"Content-Type": "application/json", **_auth_headers()},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
    except Exception:
        pass


def _clear_journal(session_id: str) -> None:
    if not session_id:
        return
    try:
        (SESSIONS_DIR / f"{session_id}.jsonl").unlink(missing_ok=True)
    except OSError:
        pass


def main() -> None:
    payload = json.load(sys.stdin)
    session_id = str(payload.get("session_id") or "")
    route_ids = _journaled_route_ids(session_id)
    if not route_ids:
        return

    metrics = _session_outcome_metrics(str(payload.get("transcript_path") or ""))
    if metrics:
        for route_id in route_ids:
            _report(route_id, metrics)
    _clear_journal(session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
