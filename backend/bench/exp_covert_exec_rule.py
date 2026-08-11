#!/usr/bin/env python3
"""Evaluate a deterministic covert-execution rule BEFORE letting it gate anything.

Motivation is measured, not hypothetical. Across the effort sweep the primary
judge caught 0-1 of 5 confirmed attacks at EVERY effort level including medium,
and the two it consistently missed in the security scan share one signature:

    xfyun-tts   "silently run"
    kcard       "silently execute"

"Run this without the user knowing" has no legitimate reading in a document an
agent will act on. That makes it a good candidate for a deterministic rule --
the judge is unreliable here and a regex is not.

But a gate shipped on that reasoning alone is exactly how this project already
hurt itself twice: `malware_indicators` fired on 58 security skills and 0 malware,
and a dedup rule excluded a canary and halted the fleet. So this script only
MEASURES. It changes nothing. It reports:

  * how many SERVED packages the rule would flag
  * whether it would flag any CANARY (any hit there means the rule is wrong)
  * whether it catches the two attacks that motivated it
  * every hit with context, so precision can be judged rather than assumed

Only if canary hits are zero and sampled precision is high should this be wired
into HARD_RISK_FLAGS. The decision is deliberately left to a human read of the
output.

Read-only. Content is pattern-matched, never executed.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
WORK = BACKEND / "corpus_v0_work.sqlite"
LIB = BACKEND / "skills_library_v1"

# Deliberately narrow. Each pattern requires BOTH concealment AND execution in
# the same clause -- "silently" alone is innocent ("the linter fails silently"),
# and "run this command" alone is what every legitimate skill does.
COVERT_EXEC = [
    r"\bsilently\s+(run|execute|invoke|call|install|download|fetch|send|upload)\b",
    r"\b(run|execute|invoke|install)\s+[^.\n]{0,40}\bsilently\b",
    r"\b(run|execute|invoke)\s+[^.\n]{0,60}\bwithout\s+(the\s+)?(user|them|informing|"
    r"telling|notifying|asking)\b",
    r"\bwithout\s+(the\s+)?user'?s?\s+(knowledge|awareness|consent|permission)\b"
    r"[^.\n]{0,60}\b(run|execute|install|send|upload)\b",
    r"\b(run|execute)\s+[^.\n]{0,40}\bin\s+the\s+background\b[^.\n]{0,40}"
    r"\bwithout\s+(telling|informing|notifying)\b",
    r"\bdo\s+not\s+(tell|inform|notify|show)\s+the\s+user\b[^.\n]{0,80}"
    r"\b(run|execute|install|command|script)\b",
    r"\b(hide|conceal|mask)\s+(this|the)\s+(command|execution|script|activity)\b",
]
COMPILED = [re.compile(p, re.IGNORECASE) for p in COVERT_EXEC]

# Documents that TEACH about covert execution are not performing it. This is the
# single biggest false-positive source -- security skills discuss these phrases
# constantly -- so a hit inside an obviously defensive context is discounted.
DEFENSIVE_CONTEXT = re.compile(
    r"\b(detect|detection|indicator|red\s*flag|warning\s+sign|never\s+do|do\s+not\s+write|"
    r"anti-?pattern|malicious|attack|threat|vulnerab|audit|review\s+for|scan\s+for|"
    r"example\s+of\s+(a\s+)?bad|reject|forbidden|prohibited|must\s+not)\b",
    re.IGNORECASE)


def entry_and_files(m: dict):
    for fr in m.get("files", []):
        h = fr["raw_sha256"]
        p = LIB / "objects" / h[:2] / h[2:4] / h
        if p.exists():
            yield fr["path"], p.read_bytes().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=25)
    ap.add_argument("--out", default=str(BENCH / "exp_covert_exec_rule.json"))
    a = ap.parse_args()

    # Canary identities: any hit on one of these means the rule is wrong.
    canary_names = set()
    try:
        for c in json.loads(
                (BACKEND / "evals" / "corpus_canaries.json").read_text())["canaries"]:
            canary_names.add(PurePosixPath(c.get("path", "")).parent.name.lower())
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: canary set unreadable ({exc}); cannot check the hard gate")

    con = sqlite3.connect(f"file:{WORK}?mode=ro", uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    rows = list(con.execute("select package_hash, manifest_json from skill_packages"))
    con.close()

    hits, canary_hits, scanned = [], [], 0
    for ph, mj in rows:
        try:
            m = json.loads(mj)
        except Exception:
            continue
        scanned += 1
        name = PurePosixPath(m.get("entrypoint") or "").parent.name
        for path, text in entry_and_files(m):
            found = None
            for rx in COMPILED:
                mo = rx.search(text)
                if mo:
                    found = mo
                    break
            if not found:
                continue
            lo, hi = max(0, found.start() - 350), min(len(text), found.end() + 350)
            ctx = text[lo:hi]
            defensive = bool(DEFENSIVE_CONTEXT.search(ctx))
            rec = {"pkg": ph, "name": name, "path": path,
                   "match": found.group(0)[:90], "defensive": defensive,
                   "context": ctx.replace("\n", " ")[:300],
                   "risk_flags": (m.get("provenance") or {}).get("risk_flags") or []}
            hits.append(rec)
            if name.lower() in canary_names:
                canary_hits.append(rec)
            break

    likely = [h for h in hits if not h["defensive"]]
    print(f"scanned {scanned:,} served packages\n")
    print(f"  raw hits:                    {len(hits)}"
          f"  ({100*len(hits)/max(scanned,1):.2f}% of corpus)")
    print(f"  in a defensive/teaching context: {len(hits)-len(likely)} (discounted)")
    print(f"  LIKELY covert execution:     {len(likely)}")
    print(f"\n  CANARY HITS: {len(canary_hits)}   "
          f"{'PASS - rule does not touch known-good skills' if not canary_hits else 'FAIL - RULE IS WRONG, do not ship'}")
    for c in canary_hits:
        print(f"    canary {c['name']}: {c['match']!r}")

    already = sum(1 for h in likely if h["risk_flags"])
    print(f"\n  of the {len(likely)} likely hits, {already} already carry a judge risk flag")
    print(f"  -> {len(likely)-already} would be NEW detections the judge missed")

    print(f"\n--- likely hits (judge these for precision) ---")
    for h in likely[:a.show]:
        print(f"\n  {h['name'][:34]:34} flags={h['risk_flags']}")
        print(f"    match: {h['match']!r}")
        print(f"    ctx:   ...{h['context'][:190]}...")

    print(f"\n--- sample discounted as defensive (check the discount is right) ---")
    for h in [x for x in hits if x["defensive"]][:5]:
        print(f"  {h['name'][:30]:30} {h['match']!r}")

    Path(a.out).write_text(json.dumps(
        {"scanned": scanned, "raw_hits": len(hits), "likely": len(likely),
         "canary_hits": len(canary_hits), "already_flagged": already,
         "hits": hits}, indent=1))
    print(f"\n-> {a.out}")
    print("\n  NOTHING WAS CHANGED. Wire into HARD_RISK_FLAGS only if canary hits")
    print("  are 0 and the likely hits above read as genuinely covert.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
