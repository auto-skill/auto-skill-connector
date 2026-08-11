#!/usr/bin/env python3
"""What would judging the corpus on Haiku actually cost?

Luna is blacked out until Aug 11 (codex weekly cap). Before spending Sami's
Claude budget on a 4-day failover, measure the real per-skill cost on a
production-shaped call rather than guessing from list prices.

Deliberately frugal: ONE batched call of the production size (32 skills, the
real enrichment_prompt_v2_batched.md, real skill bytes rebuilt with the
production build_judge_input). That is enough to get tokens/skill and $/skill,
and it costs about as much as a single production judge call.

`claude -p --output-format json` returns the exact usage block the Discord
`!usage` command aggregates, so these numbers are the same ones the bot reports.

Skill content is UNTRUSTED: delimited blocks, stdin only, no tools.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
sys.path.insert(0, str(BENCH))
from run2_enrich import build_judge_input  # noqa: E402

CLAUDE = "/home/sami/.npm-global/bin/claude"
BATCHED_PROMPT = BENCH / "enrichment_prompt_v2_batched.md"
EDB = BACKEND / "enrichment_v1.db"


def load_fetch_caches() -> dict:
    out = {}
    def bno(p):
        m = re.search(r"run2_fetch_b(\d+)\.json$", str(p))
        return int(m.group(1)) if m else -1
    for f in sorted(BENCH.glob("run2_fetch_b*.json"), key=bno):
        try:
            d = json.loads(f.read_text())
            if isinstance(d, dict):
                out.update({k: v for k, v in d.items() if isinstance(v, dict)})
        except Exception:
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=32, help="skills in the one call")
    a = ap.parse_args()

    cache = load_fetch_caches()
    con = sqlite3.connect(f"file:{EDB}?mode=ro", uri=True)
    con.execute("pragma busy_timeout=60000")
    rows = con.execute(
        """select skill_id from enrichments
           where judge_role='primary' and prompt_version='v2.1'
             and skill_id is not null order by rowid desc limit 4000""").fetchall()
    con.close()

    items = []
    for (sid,) in rows:
        f = cache.get(sid)
        if isinstance(f, dict) and f.get("status") == "ok" and f.get("entry_hash"):
            items.append((sid, f))
        if len(items) >= a.n:
            break
    if len(items) < a.n:
        print(f"  only {len(items)} usable skills found")
    print(f"  building one production-shaped call with {len(items)} skills", flush=True)

    parts = [BATCHED_PROMPT.read_text(encoding="utf-8")]
    for idx, (sid, f) in enumerate(items, 1):
        block, _nh, _tr = build_judge_input({"id": sid}, f)
        block = block.replace("<<<UNTRUSTED_SKILL_DATA>>>",
                              f"<<<UNTRUSTED_SKILL_DATA id={idx}>>>")
        block = block.replace("<<<END_UNTRUSTED_SKILL_DATA>>>",
                              f"<<<END_UNTRUSTED_SKILL_DATA id={idx}>>>")
        parts.append("\n" + block)
    prompt = "\n".join(parts)
    print(f"  prompt chars: {len(prompt):,}", flush=True)

    t0 = time.time()
    p = subprocess.run([CLAUDE, "-p", "--model", "haiku", "--output-format", "json"],
                       input=prompt, capture_output=True, text=True, timeout=900)
    el = time.time() - t0
    try:
        payload = json.loads(p.stdout or "{}")
    except Exception:
        print("  could not parse claude json output:", (p.stdout or "")[:300])
        return 1

    u = payload.get("usage") or {}
    cost = payload.get("total_cost_usd")
    inp = u.get("input_tokens", 0) or 0
    outp = u.get("output_tokens", 0) or 0
    cc = u.get("cache_creation_input_tokens", 0) or 0
    cr = u.get("cache_read_input_tokens", 0) or 0
    billed_in = inp + cc + cr

    # did it actually produce verdicts? a cheap call that answers nothing is not cheap
    body = payload.get("result") or ""
    n_verdicts = 0
    try:
        m = re.search(r'\{.*"verdicts".*\}', body, re.S)
        if m:
            n_verdicts = len(json.loads(m.group(0)).get("verdicts") or [])
    except Exception:
        pass

    n = max(len(items), 1)
    print(f"\n  === ONE PRODUCTION-SHAPED HAIKU CALL ({n} skills) ===")
    print(f"    wall clock          {el:>10.1f}s")
    print(f"    input tokens        {inp:>10,}")
    print(f"    cache creation      {cc:>10,}")
    print(f"    cache read          {cr:>10,}")
    print(f"    output tokens       {outp:>10,}")
    print(f"    total billed input  {billed_in:>10,}")
    print(f"    verdicts returned   {n_verdicts:>10,}/{n}")
    if cost is not None:
        print(f"    total_cost_usd      ${cost:>9.4f}")
        print(f"\n    per skill: {billed_in/n:,.0f} in + {outp/n:,.0f} out tokens, ${cost/n:.5f}")
        for label, count in (("remaining backlog", 694_702), ("full corpus", 804_608)):
            print(f"    {label:<20} {count:>9,} skills -> ${cost/n*count:>10,.2f}")
    Path(BENCH / "haiku_cost_probe.json").write_text(json.dumps(
        {"skills": n, "seconds": round(el, 1), "input_tokens": inp,
         "cache_creation": cc, "cache_read": cr, "output_tokens": outp,
         "billed_input": billed_in, "verdicts": n_verdicts,
         "total_cost_usd": cost}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
