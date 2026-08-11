#!/usr/bin/env python3
"""How many skills can the Claude plan actually judge? Measured, not extrapolated.

The earlier probe priced one batch at API list rates and multiplied out to
$6,602. That is the wrong unit for a subscription: on a Max plan you spend PLAN
QUOTA, not dollars, and the two do not convert. A single batch also moved the
5-hour meter by exactly 1 integer point, which is far too coarse to divide by --
the true cost could be anywhere in 0.5-1.5 points.

So: run enough real batches to move the meter well past its rounding, sampling
utilization as we go, and derive skills-per-point directly.

Both meters matter and they are very different:
  five_hour  refills every 5h  -- governs burst rate
  seven_day  refills weekly    -- governs total volume, and is the binding one

Production-shaped throughout: real batched prompt, real skill bytes via
build_judge_input, 32 skills per call, concurrency like the pipeline's.
Skill content is UNTRUSTED -- delimited blocks, stdin only, no tools.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
sys.path.insert(0, str(BENCH))
from run2_enrich import build_judge_input  # noqa: E402

CLAUDE = "/home/sami/.npm-global/bin/claude"
BATCHED_PROMPT = BENCH / "enrichment_prompt_v2_batched.md"
EDB = BACKEND / "enrichment_v1.db"


def _token() -> str | None:
    try:
        d = json.loads((Path.home() / ".claude/.credentials.json").read_text())
    except Exception:
        return None
    def dig(o):
        if isinstance(o, dict):
            for k in ("accessToken", "access_token"):
                if isinstance(o.get(k), str):
                    return o[k]
            for v in o.values():
                r = dig(v)
                if r:
                    return r
        return None
    return dig(d)


def utilization() -> dict:
    tok = _token()
    if not tok:
        return {}
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
        b = json.load(urllib.request.urlopen(req, timeout=25))
        return {k: (b.get(k) or {}).get("utilization") for k in ("five_hour", "seven_day")}
    except Exception:
        return {}


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
    ap.add_argument("--batches", type=int, default=12)
    ap.add_argument("--per-batch", type=int, default=32)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    cache = load_fetch_caches()
    con = sqlite3.connect(f"file:{EDB}?mode=ro", uri=True)
    con.execute("pragma busy_timeout=60000")
    rows = con.execute(
        """select skill_id from enrichments
           where judge_role='primary' and prompt_version='v2.1'
             and skill_id is not null order by rowid desc limit 60000""").fetchall()
    con.close()
    need = a.batches * a.per_batch
    pool = []
    seen = set()
    for (sid,) in rows:
        if sid in seen:
            continue
        f = cache.get(sid)
        if isinstance(f, dict) and f.get("status") == "ok" and f.get("entry_hash"):
            pool.append((sid, f)); seen.add(sid)
        if len(pool) >= need:
            break
    print(f"  usable skills: {len(pool):,} (want {need:,})", flush=True)
    groups = [pool[i:i + a.per_batch] for i in range(0, len(pool), a.per_batch)][:a.batches]

    prompt_head = BATCHED_PROMPT.read_text(encoding="utf-8")

    def build(group):
        parts = [prompt_head]
        for idx, (sid, f) in enumerate(group, 1):
            block, _nh, _tr = build_judge_input({"id": sid}, f)
            block = block.replace("<<<UNTRUSTED_SKILL_DATA>>>",
                                  f"<<<UNTRUSTED_SKILL_DATA id={idx}>>>")
            block = block.replace("<<<END_UNTRUSTED_SKILL_DATA>>>",
                                  f"<<<END_UNTRUSTED_SKILL_DATA id={idx}>>>")
            parts.append("\n" + block)
        return "\n".join(parts)

    def run(group):
        try:
            p = subprocess.run([CLAUDE, "-p", "--model", "haiku",
                                "--output-format", "json"],
                               input=build(group), capture_output=True,
                               text=True, timeout=900)
            d = json.loads(p.stdout or "{}")
        except Exception as exc:
            return {"err": str(exc)[:80], "skills": len(group)}
        u = d.get("usage") or {}
        body = d.get("result") or ""
        nv = 0
        try:
            m = re.search(r'\{.*"verdicts".*\}', body, re.S)
            if m:
                nv = len(json.loads(m.group(0)).get("verdicts") or [])
        except Exception:
            pass
        return {"skills": len(group), "verdicts": nv,
                "in": (u.get("input_tokens", 0) or 0)
                      + (u.get("cache_creation_input_tokens", 0) or 0)
                      + (u.get("cache_read_input_tokens", 0) or 0),
                "out": u.get("output_tokens", 0) or 0,
                "cost": d.get("total_cost_usd") or 0.0}

    before = utilization()
    print(f"  BEFORE  5h={before.get('five_hour')}%  7d={before.get('seven_day')}%", flush=True)
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(run, groups), 1):
            results.append(r)
            if i % 4 == 0:
                u = utilization()
                print(f"    after {i:>2} batches  5h={u.get('five_hour')}%  "
                      f"7d={u.get('seven_day')}%", flush=True)
    el = time.time() - t0
    time.sleep(45)                    # let the meter settle
    after = utilization()

    ok = [r for r in results if "err" not in r]
    skills = sum(r["skills"] for r in ok)
    verdicts = sum(r.get("verdicts", 0) for r in ok)
    tin = sum(r["in"] for r in ok)
    tout = sum(r["out"] for r in ok)
    cost = sum(r["cost"] for r in ok)
    d5 = (after.get("five_hour") or 0) - (before.get("five_hour") or 0)
    d7 = (after.get("seven_day") or 0) - (before.get("seven_day") or 0)

    print(f"\n  === MEASURED CAPACITY ===")
    print(f"    batches run          {len(ok)}/{len(groups)}")
    print(f"    skills judged        {skills:,}")
    print(f"    verdicts returned    {verdicts:,}")
    print(f"    wall clock           {el:,.0f}s ({skills/max(el,1)*3600:,.0f} skills/h at {a.workers} workers)")
    print(f"    billed input         {tin:,}")
    print(f"    output               {tout:,}")
    print(f"    api-list cost        ${cost:.4f}")
    print(f"\n    5-hour meter  {before.get('five_hour')}% -> {after.get('five_hour')}%   (+{d5})")
    print(f"    7-day meter   {before.get('seven_day')}% -> {after.get('seven_day')}%   (+{d7})")
    if d5 > 0:
        print(f"\n    skills per 5h point:  {skills/d5:,.0f}")
        print(f"    a full 5h window (100 pts) ~= {skills/d5*100:,.0f} skills")
    if d7 > 0:
        print(f"    skills per 7d point:  {skills/d7:,.0f}")
        print(f"    8% of 7d remaining    ~= {skills/d7*8:,.0f} skills")
    else:
        print(f"\n    7-day meter did not move for {skills:,} skills --")
        print(f"    so >{skills:,} skills fit inside ONE 7d point (<1% of the week)")
    Path(BENCH / "haiku_capacity.json").write_text(json.dumps(
        {"skills": skills, "verdicts": verdicts, "seconds": round(el),
         "billed_input": tin, "output": tout, "api_cost": cost,
         "before": before, "after": after, "d5": d5, "d7": d7}, indent=1),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
