#!/usr/bin/env python3
"""Where is the judging ceiling -- our concurrency setting, or the API?

Batch profiling says the primary stage is exactly concurrency-bound: 100 calls
at CONCURRENCY=6 and ~47s/call predicts 783s, and the measured median is 753s.
So the stage is doing precisely what it was told to do, and the open question is
whether it was told the right number.

Two very different worlds, and they need different fixes:

  latency-bound   each call costs ~47s no matter how many run at once. Then
                  throughput scales ~linearly with concurrency and the setting
                  is simply too low.
  capacity-bound  per-call latency rises as concurrency rises (server-side
                  queueing, or local CPU/memory pressure from N node processes
                  on a 4-core box). Then raising it buys nothing and risks
                  timeouts and 429s.

Measures aggregate throughput (calls/sec) at several concurrency levels using
REAL batched judge prompts, and records per-call latency plus host load at each
level so local saturation is distinguishable from server-side pushback.

Deliberately conservative: this runs ALONGSIDE the live fleet, which is itself
using 6 concurrent slots. Every level here is additional load, so levels are
kept short and any error (429/timeout/rc!=0) is reported loudly -- a throughput
number bought with failures is not throughput. If errors appear at a level, that
level is the ceiling regardless of what its raw timing says.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics as st
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
import run2_enrich as E  # noqa: E402
import exp_effort_sweep as S  # noqa: E402


def one_call(prompt: str, timeout: int = 900) -> dict:
    jail = tempfile.mkdtemp(prefix="conc-")
    t0 = time.time()
    try:
        p = subprocess.run(
            [E.CODEX_BIN, "exec", "--ephemeral", "--ignore-user-config",
             "--skip-git-repo-check", "-s", "read-only", "-C", jail,
             "-m", E.LUNA_MODEL, "-c", f'model_reasoning_effort="{E.LUNA_EFFORT}"', "-"],
            input=prompt,
            env={"HOME": "/home/sami", "PATH": "/usr/bin:/bin",
                 "CODEX_HOME": E.CODEX_HOME, "TERM": "dumb"},
            capture_output=True, text=True, timeout=timeout)
        blob = ((p.stdout or "") + (p.stderr or "")).lower()
        throttled = any(k in blob for k in ("rate limit", "429", "usage limit",
                                            "overloaded", "too many requests"))
        return {"secs": time.time() - t0, "rc": p.returncode,
                "throttled": throttled,
                "parsed": E.parse_judge_json(p.stdout or "") is not None,
                "err": (p.stderr or "")[-160:] if p.returncode else ""}
    except subprocess.TimeoutExpired:
        return {"secs": time.time() - t0, "rc": -9, "throttled": False,
                "parsed": False, "err": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"secs": time.time() - t0, "rc": -1, "throttled": False,
                "parsed": False, "err": f"{type(exc).__name__}"}
    finally:
        shutil.rmtree(jail, ignore_errors=True)


def loadavg() -> float:
    return os.getloadavg()[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="4,8,12")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--out", default=str(BENCH / "exp_concurrency_scaling.json"))
    a = ap.parse_args()

    levels = [int(x) for x in a.levels.split(",") if x.strip()]
    sample = S.build_sample(60, 5)
    pool = [s for s in sample if s["group"] in ("medium_accepted", "canary")]
    if len(pool) < a.batch_size * max(levels):
        pool = (pool * 10)[: a.batch_size * max(levels)]
    prompt_tpl = (BENCH / "enrichment_prompt_v2_batched.md").read_text()

    def build_prompt(chunk):
        parts = [prompt_tpl]
        for idx, s in enumerate(chunk, 1):
            inner = s["block"].replace("<<<UNTRUSTED_SKILL_DATA>>>",
                                       f"<<<UNTRUSTED_SKILL_DATA id={idx}>>>")
            inner = inner.replace("<<<END_UNTRUSTED_SKILL_DATA>>>",
                                  f"<<<END_UNTRUSTED_SKILL_DATA id={idx}>>>")
            parts.append("\n" + inner)
        return "\n".join(parts)

    print(f"fleet is running at CONCURRENCY={os.environ.get('AUTOSKILL_CONCURRENCY','6')}; "
          f"these levels are ADDITIONAL load\n", flush=True)
    results = {}
    for lvl in levels:
        prompts = [build_prompt(pool[i * a.batch_size:(i + 1) * a.batch_size])
                   for i in range(lvl)]
        prompts = [p for p in prompts if p.strip()]
        l0 = loadavg()
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=lvl) as ex:
            rs = list(ex.map(one_call, prompts))
        wall = time.time() - t0
        l1 = loadavg()
        lat = [r["secs"] for r in rs]
        bad = [r for r in rs if r["rc"] != 0]
        thr = [r for r in rs if r["throttled"]]
        ok_parsed = sum(1 for r in rs if r["parsed"])
        results[lvl] = {
            "n_calls": len(rs), "wall_s": round(wall, 1),
            "median_latency": round(st.median(lat), 1),
            "p95_latency": round(sorted(lat)[max(0, int(len(lat) * 0.95) - 1)], 1),
            "calls_per_min": round(60 * len(rs) / wall, 2),
            "skills_per_hr": round(3600 * len(rs) * a.batch_size / wall),
            "errors": len(bad), "throttled": len(thr), "parsed": ok_parsed,
            "load_before": round(l0, 1), "load_after": round(l1, 1),
        }
        r = results[lvl]
        print(f"  concurrency {lvl:>3}: wall {r['wall_s']:>6}s  "
              f"median {r['median_latency']:>5}s  p95 {r['p95_latency']:>5}s  "
              f"{r['calls_per_min']:>5} calls/min  ~{r['skills_per_hr']:>6,} skills/hr  "
              f"err={r['errors']} throttled={r['throttled']} parsed={r['parsed']}/{r['n_calls']}  "
              f"load {r['load_before']}->{r['load_after']}", flush=True)
        if bad:
            print(f"      first error: {bad[0]['err'][:120]}", flush=True)

    print("\n=== READING ===")
    base = results[levels[0]]
    for lvl in levels[1:]:
        r = results[lvl]
        ideal = base["calls_per_min"] * (lvl / levels[0])
        eff = 100 * r["calls_per_min"] / ideal if ideal else 0
        lat_infl = 100 * (r["median_latency"] / base["median_latency"] - 1)
        verdict = ("LATENCY-BOUND: scales, raise it" if eff > 80 and lat_infl < 25
                   else "CAPACITY-BOUND: queueing, do not raise" if eff < 60 or lat_infl > 50
                   else "partial scaling")
        print(f"  {levels[0]} -> {lvl}: {eff:.0f}% of ideal scaling, "
              f"latency +{lat_infl:.0f}%  -> {verdict}")
    if any(r["throttled"] or r["errors"] for r in results.values()):
        print("\n  NOTE: errors/throttling observed. The highest CLEAN level is the")
        print("  real ceiling -- throughput bought with failures is not throughput.")

    Path(a.out).write_text(json.dumps(results, indent=1))
    print(f"\n-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
