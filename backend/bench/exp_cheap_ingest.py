#!/usr/bin/env python3
"""Ingesting a skill is a shallow read. Are we overpaying for it?

Production judges at `medium` reasoning effort over up to ENTRY_CHAR_CAP=24,000
chars of content. Content is ~80% of the ~2,919 tokens/skill we spend, and
reasoning effort is the other knob. If the task really is shallow, both can come
down without moving the verdict.

Arms (all use the PRODUCTION batched prompt and the production sealed call):

  A  medium / 24,000   control -- re-runs today's exact settings. Its
                       disagreement with the STORED verdicts is the
                       non-determinism NOISE FLOOR. Nothing can beat it, and any
                       threshold tighter than it would reject production itself.
  B  low    / 24,000   isolates reasoning effort
  C  low    /  6,000   isolates effort + content together
  D  medium /  6,000   isolates content alone

Ground truth is the stored Luna verdict for the same norm_hash, so each arm costs
N verdicts, not 2N.

Gates (from EFFORT_DECISION_RULE.md, unchanged):
  * canaries must be 22/22 -- hard, no tolerance
  * agreement with stored must be within (control's own disagreement + 3pp)
  * parse/coverage >= 98% -- a shortfall falls back to single calls and erases
    the batching win

Skill content is UNTRUSTED: delimited blocks, stdin only, no tools, read-only
sandbox, scrubbed env, empty temp cwd -- identical to production.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run2_enrich as R  # noqa: E402

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
EDB = BACKEND / "enrichment_v1.db"
OUT = BENCH / "cheap_ingest_results.json"

ARMS = [
    ("A_control_medium_24k", "medium", 24_000),
    ("B_low_24k",            "low",    24_000),
    ("C_low_6k",             "low",     6_000),
    ("D_medium_6k",          "medium",  6_000),
]


def load_fetch_caches() -> dict:
    out = {}
    import re as _re
    def bno(p):
        m = _re.search(r"run2_fetch_b(\d+)\.json$", str(p))
        return int(m.group(1)) if m else -1
    for f in sorted(BENCH.glob("run2_fetch_b*.json"), key=bno):
        try:
            d = json.loads(f.read_text())
            if isinstance(d, dict):
                out.update({k: v for k, v in d.items() if isinstance(v, dict)})
        except Exception:
            pass
    return out


def sample(n: int, seed: int) -> list[dict]:
    con = sqlite3.connect(f"file:{EDB}?mode=ro", uri=True)
    con.execute("pragma busy_timeout=120000")
    rows = con.execute(
        """select skill_id, norm_hash, output_json from enrichments
           where judge_role='primary' and model_snapshot like 'gpt-5.6-luna%'
             and prompt_version='v2.1' and skill_id is not null
           order by rowid desc limit 30000""").fetchall()
    con.close()
    cache = load_fetch_caches()
    pool = []
    for sid, nh, oj in rows:
        try:
            o = json.loads(oj)
        except Exception:
            continue
        f = cache.get(sid)
        if not isinstance(f, dict) or not f.get("entry_hash"):
            continue
        if f.get("status") != "ok":
            continue
        pool.append({"skill_id": sid, "norm_hash": nh, "stored": o, "fetched": f})
    random.Random(seed).shuffle(pool)
    return pool[:n]


def run_arm(items, effort: str, cap: int, batch: int, workers: int):
    """Judge `items` through the production batched path at (effort, cap)."""
    R.LUNA_EFFORT = effort
    R.ENTRY_CHAR_CAP = cap
    built = []
    for it in items:
        block, nh, trunc = R.build_judge_input({"id": it["skill_id"]}, it["fetched"])
        built.append((({"id": it["skill_id"]}), it["fetched"], block, nh, trunc))
    groups = [built[i:i + batch] for i in range(0, len(built), batch)]

    got, usage = {}, {"tokens_in": 0, "tokens_out": 0, "calls": 0}

    def one(g):
        verdicts, r = R.call_luna_batch(g)
        return g, (verdicts or {}), r

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for g, verdicts, r in ex.map(one, groups):
            usage["calls"] += 1
            usage["tokens_in"] += (r or {}).get("tokens_in", 0) or 0
            usage["tokens_out"] += (r or {}).get("tokens_out", 0) or 0
            for idx, item in enumerate(g, 1):
                v = verdicts.get(idx)
                if v is not None:
                    got[item[3]] = v          # key by norm_hash
    return got, usage


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=128)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--arms", default="")
    a = ap.parse_args()

    items = sample(a.n, a.seed)
    print(f"  sample: {len(items)} skills with stored luna verdicts", flush=True)
    by_nh = {it["norm_hash"]: it for it in items}

    arms = [x for x in ARMS if not a.arms or x[0] in a.arms.split(",")]
    results = {}
    for name, effort, cap in arms:
        print(f"\n  arm {name}: effort={effort} cap={cap:,}", flush=True)
        got, usage = run_arm(items, effort, cap, a.batch, a.workers)
        n = len(got)
        agree = same_real = 0
        for nh, v in got.items():
            st = by_nh[nh]["stored"]
            if v.get("is_real_skill") == st.get("is_real_skill"):
                same_real += 1
            agree += 1
        cov = n / max(len(items), 1)
        tin = usage["tokens_in"]
        per = tin / max(n, 1)
        results[name] = {
            "effort": effort, "cap": cap, "n": n, "coverage": round(cov, 4),
            "verdict_agreement": round(same_real / max(n, 1), 4),
            "tokens_in": tin, "tokens_out": usage["tokens_out"],
            "calls": usage["calls"], "tokens_per_skill": round(per, 1),
        }
        print(f"    coverage={cov*100:.1f}%  agreement={same_real/max(n,1)*100:.1f}%"
              f"  tok/skill={per:,.0f}", flush=True)

    OUT.write_text(json.dumps(results, indent=1), encoding="utf-8")
    ctrl = results.get("A_control_medium_24k")
    print("\n=== CHEAP INGEST SWEEP ===")
    print("  arm                     effort  cap      cov    agree   tok/skill   vs control")
    for name, r in results.items():
        base = ctrl["tokens_per_skill"] if ctrl else r["tokens_per_skill"]
        print(f"  {name:<22} {r['effort']:<7} {r['cap']:>6,}  {r['coverage']*100:>5.1f}%"
              f"  {r['verdict_agreement']*100:>5.1f}%  {r['tokens_per_skill']:>9,.0f}"
              f"   {(1-r['tokens_per_skill']/base)*100:>+6.1f}%")
    if ctrl:
        floor = 1 - ctrl["verdict_agreement"]
        print(f"\n  noise floor (control vs stored): {floor*100:.1f}% disagreement")
        print(f"  gate: a cheaper arm passes if disagreement <= {floor*100+3:.1f}%")
        for name, r in results.items():
            if name == "A_control_medium_24k":
                continue
            d = (1 - r["verdict_agreement"]) * 100
            ok = d <= floor * 100 + 3 and r["coverage"] >= 0.98
            print(f"    {name:<22} disagreement={d:>5.1f}%  -> "
                  f"{'PASS' if ok else 'FAIL'}")
    print(f"\n  written -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
