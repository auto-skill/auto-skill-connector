#!/usr/bin/env python3
"""Automated secondary judge for the unattended nightly enrichment unit.

Interactive runs used the orchestrator's subagent mechanism for the Haiku
second opinion. A daemon has no orchestrator, so this drives the same judge
headlessly via `claude -p --model haiku`, reading the queue that
`run2_enrich.py --stage primary` emits and writing the results file that
`--stage secondary-ingest` consumes. Same prompt, same schema, same model.

Hostile-data discipline is preserved: the skill content is passed inside the
same delimited block, delivered on the child's STDIN (never as argv, never via
a file the model must fetch), and the judge uses no tools at all. Nothing in
the content can reach a shell.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BENCH = Path(__file__).resolve().parent
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/sami/.npm-global/bin/claude")
HAIKU_SNAPSHOT = "claude-haiku-4-5-20251001"
SECONDARY_PROMPT = BENCH / "enrichment_prompt_v2_secondary.md"
CALL_TIMEOUT = 300
SECONDARY_UNAVAILABLE_MARKERS = (
    "oauth session expired and could not be refreshed",
    "you've hit your weekly limit",
    "you've hit your usage limit",
    "provider unavailable: weekly-limit cooldown",
)
TOKEN_FILE = Path(os.environ.get(
    "AUTOSKILL_CLAUDE_TOKEN_FILE",
    "/srv/mobile-codex/autoskill-daemon/private/claude-setup-token.env"))
BLACKOUT_FILE = Path(os.environ.get(
    "AUTOSKILL_HAIKU_BLACKOUT_FILE",
    "/srv/mobile-codex/autoskill-daemon/state/haiku_blackout_until"))


def provider_unavailable(raw: str) -> bool:
    """Return true only when Claude never reached the judge prompt.

    This is infrastructure state, not malformed output from an evaluated skill.
    Keeping that distinction lets the combiner defer a primary reject until the
    independent judge is available again instead of treating an expired OAuth
    session as evidence about the skill.
    """
    text = (raw or "").casefold()
    return any(marker in text for marker in SECONDARY_UNAVAILABLE_MARKERS)


def claude_env() -> dict[str, str]:
    """Use a durable setup token when installed without exposing it to the unit.

    The token stays in a mode-0600 file and is passed only to the headless
    Claude subprocess. The normal Claude credentials remain the fallback.
    """
    env = dict(os.environ)
    try:
        for line in TOKEN_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                token = line.partition("=")[2].strip()
                if token:
                    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
                break
    except OSError:
        pass
    return env


def blackout_until() -> float:
    try:
        until = float(BLACKOUT_FILE.read_text(encoding="ascii").strip())
        return until if until > time.time() else 0.0
    except (OSError, ValueError):
        return 0.0


def set_weekly_blackout(raw: str) -> float:
    """Persist Claude's own weekly-reset time so later batches make no calls."""
    now = datetime.now(ZoneInfo("America/Denver"))
    match = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(america/denver\)",
                      raw, re.IGNORECASE)
    if match:
        hour, minute, meridiem = int(match[1]), int(match[2] or 0), match[3].lower()
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
        reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset <= now:
            reset += timedelta(days=1)
        until = reset.timestamp()
    else:
        # A malformed provider message must not make every batch re-probe it.
        until = time.time() + 3600
    BLACKOUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = BLACKOUT_FILE.with_suffix(".tmp")
    temp.write_text(f"{until:.0f}\n", encoding="ascii")
    os.replace(temp, BLACKOUT_FILE)
    return until


def parse_json(raw: str) -> dict | None:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"\A```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*\Z", "", s).strip()
    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return v
    except Exception:
        pass
    depth, start = 0, None
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    v = json.loads(s[start:i + 1])
                    if isinstance(v, dict):
                        return v
                except Exception:
                    pass
                start = None
    return None


def judge(prompt_text: str) -> tuple[dict | None, str]:
    """One headless Haiku call, prompt delivered on STDIN.

    Stdin rather than a file-read is deliberate. Headless `claude -p` cannot use
    the Read tool without an interactive permission grant (it replies "please
    approve the permission prompt" and judges nothing), and piping the prompt in
    means the judge needs **no tools at all** — which is exactly the discipline
    we want for hostile data. Nothing from the skill content ever reaches a
    shell: it is passed as process stdin, never as argv, never via a file the
    model must fetch.
    """
    try:
        p = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", "haiku"],
            input=prompt_text, capture_output=True, text=True,
            timeout=CALL_TIMEOUT, env=claude_env())
        return parse_json(p.stdout), (p.stdout or "")[:2000]
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:  # never crash the daemon
        return None, f"error: {e}"[:300]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--max-calls", type=int, default=150)
    args = ap.parse_args()

    if not args.queue.exists():
        print("no queue file; nothing to do")
        args.out.write_text(json.dumps({"results": []}), encoding="utf-8")
        return 0
    q = json.loads(args.queue.read_text(encoding="utf-8"))
    items = q.get("items", [])[:args.max_calls]
    if not items:
        args.out.write_text(json.dumps({"run_id": q.get("run_id"),
                                        "model_snapshot": HAIKU_SNAPSHOT,
                                        "results": []}), encoding="utf-8")
        print("queue empty")
        return 0

    until = blackout_until()
    if until:
        results = [{"norm_hash": it["norm_hash"],
                    "raw": "provider unavailable: weekly-limit cooldown",
                    "tokens_in": 0, "tokens_out": 0}
                   for it in items]
        args.out.write_text(json.dumps({"run_id": q.get("run_id"),
                                        "model_snapshot": HAIKU_SNAPSHOT,
                                        "availability": "unavailable",
                                        "results": results}, indent=1), encoding="utf-8")
        print(f"haiku blackout: skipping {len(items)} calls for {max(0, until-time.time()):.0f}s")
        return 0

    base = SECONDARY_PROMPT.read_text(encoding="utf-8")
    # Independent headless calls -- parallelize. Sequentially this stage ran
    # ~26s/call and consumed up to 24% of a clean batch's wall clock (19 calls
    # = 8+ minutes). Four workers cut it to ~1/4 with no shared state beyond
    # the results list; order is preserved by indexing, not completion time.
    from concurrent.futures import ThreadPoolExecutor
    workers = int(os.environ.get("AUTOSKILL_HAIKU_CONCURRENCY", "4"))
    results: list = [None] * len(items)
    ok = bad = done = 0

    def one(idx_item):
        idx, it = idx_item
        parsed, raw = judge(base + "\n\n" + it["data_block"])
        return idx, it, parsed, raw

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for idx, it, parsed, raw in ex.map(one, enumerate(items)):
            if parsed is None:
                bad += 1
                results[idx] = {"norm_hash": it["norm_hash"], "raw": raw,
                                "tokens_in": 0, "tokens_out": 0}
            else:
                ok += 1
                results[idx] = {"norm_hash": it["norm_hash"], "output": parsed,
                                "tokens_in": 0, "tokens_out": 0}
            done += 1
            if done % 5 == 0 or done == len(items):
                print(f"  haiku {done}/{len(items)} ok={ok} unparsed={bad}", flush=True)

    unavailable = bool(results) and all(
        provider_unavailable(str(result.get("raw", "")))
        for result in results if isinstance(result, dict)
    )
    if unavailable and all("weekly limit" in str(result.get("raw", "")).casefold()
                           for result in results if isinstance(result, dict)):
        until = set_weekly_blackout(str(results[0].get("raw", "")))
        print(f"haiku weekly limit: blackout for {max(0, until-time.time()):.0f}s")
    args.out.write_text(json.dumps({"run_id": q.get("run_id"),
                                    "model_snapshot": HAIKU_SNAPSHOT,
                                    "note": "headless claude -p; no in/out token split exposed",
                                    "availability": "unavailable" if unavailable else "ok",
                                    "results": results}, indent=1), encoding="utf-8")
    print(f"haiku: {ok} ok, {bad} unparsed -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
