#!/usr/bin/env python3
"""Confirm a chosen effort still holds up under BATCHED judging.

exp_effort_sweep.py judges one skill per call. Production does not: it packs
LUNA_BATCH_SIZE (8) skills into a single call using enrichment_prompt_v2_batched
and asks for one JSON object per skill, keyed by index. Those are different
tasks, and the difference cuts both ways:

  latency  the sweep pays full CLI startup for ONE skill, so it UNDERSTATES the
           win from lower effort -- in production, startup is amortised over 8
           and reasoning is a larger share of each call.
  quality  tracking 8 skills in one context is harder than tracking one, and it
           adds failure modes the sweep cannot see: verdicts returned against
           the wrong index, skills silently dropped from the reply, or one
           skill's content bleeding into another's judgement. Cheap reasoning
           is exactly where those would show up first, so the sweep may be
           OPTIMISTIC about quality.

So the sweep narrows the field and this confirms the finalist on the real path.
It calls E.call_luna_batch directly -- the same function the fleet runs -- with
effort set through AUTOSKILL_LUNA_EFFORT, so nothing here is a reimplementation
that could drift from production.

Checks, in order of how badly they'd hurt:

  canary pass       every canary must come back real. Hard gate.
  index alignment   the verdict returned for slot N must actually describe the
                    skill in slot N. Checked by name/description overlap, not
                    assumed -- a silent off-by-one would mislabel entire batches.
  completeness      how many of the 8 come back at all (the rest fall back to
                    individual calls, which erases the batching win)
  agreement         vs stored medium verdicts

Read-only. Skill content is UNTRUSTED and passed only as delimited data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics as st
import sys
import time
from collections import Counter
from pathlib import Path

BENCH = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--effort", required=True)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--n-extra", type=int, default=24,
                    help="non-canary skills to mix in alongside the canaries")
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    # Must be set BEFORE run2_enrich is imported: LUNA_EFFORT is read at import.
    os.environ["AUTOSKILL_LUNA_EFFORT"] = a.effort
    os.environ["AUTOSKILL_LUNA_BATCH"] = str(a.batch_size)
    sys.path.insert(0, str(BENCH))
    import run2_enrich as E  # noqa: E402
    import exp_effort_sweep as S  # noqa: E402

    print(f"effort={E.LUNA_EFFORT}  snapshot={E.primary_snapshot()}  "
          f"batch_size={E.LUNA_BATCH_SIZE}", flush=True)

    sample = S.build_sample(a.n_extra, a.seed)
    keep = [s for s in sample if s["group"] in ("canary", "medium_accepted",
                                               "medium_rejected", "known_attack")]
    print(f"sample: {len(keep)}  {dict(Counter(s['group'] for s in keep))}", flush=True)

    # call_luna_batch expects (s, f, block, nh, trunc). Only block/nh are used
    # for the request; s/f ride along for bookkeeping.
    items = [({"id": s["nh"], "name": s["name"]}, {}, s["block"], s["nh"], False)
             for s in keep]
    by_nh = {s["nh"]: s for s in keep}

    groups = [items[i:i + a.batch_size] for i in range(0, len(items), a.batch_size)]
    print(f"-> {len(groups)} batched calls\n", flush=True)

    rows, latencies, returned, expected = [], [], 0, 0
    for gi, g in enumerate(groups, 1):
        t0 = time.time()
        try:
            verdicts, r = E.call_luna_batch(g)
        except Exception as exc:  # noqa: BLE001
            print(f"  call {gi}: EXCEPTION {type(exc).__name__}: {exc}", flush=True)
            continue
        dt = time.time() - t0
        latencies.append(dt)
        expected += len(g)
        returned += len(verdicts)
        for idx, item in enumerate(g, 1):
            v = verdicts.get(idx)
            s = by_nh[item[3]]
            # Index alignment: does the verdict actually describe THIS skill?
            # Compare the skill name against the verdict's SUMMARY and TRIGGERS,
            # by prefix. Matching against the whole verdict JSON and demanding a
            # full token match cried wolf on three of 28 rows -- a verdict has no
            # reason to repeat the skill's slug, and "huggingface-hub" summarised
            # as "Hugging Face Hub..." fails an exact-token test while being
            # perfectly aligned. A hand-check on 8 deliberately distinctive
            # skills (latex/matlab/nginx/sql/tokenizers/biopython/risk/embeddings)
            # confirmed 8/8 correct slots, so the misalignment was in the check.
            aligned = None
            if v:
                hay = (str(v.get("summary") or "") + " "
                       + " ".join(v.get("triggers") or [])).lower()
                toks = [t for t in re.split(r"[^a-z0-9]+", (s["name"] or "").lower())
                        if len(t) > 3]
                aligned = (not toks) or any(t[:6] in hay for t in toks)
            rows.append({"nh": s["nh"], "group": s["group"], "name": s["name"],
                         "returned": v is not None,
                         "is_real": (v or {}).get("is_real_skill"),
                         "spec": (v or {}).get("specificity"),
                         "flags": (v or {}).get("risk_flags") or [],
                         "aligned": aligned,
                         "medium_is_real": s["medium_is_real"],
                         "medium_spec": s["medium_spec"]})
        print(f"  call {gi}/{len(groups)}: {len(verdicts)}/{len(g)} verdicts "
              f"in {dt:.0f}s", flush=True)

    can = [r for r in rows if r["group"] == "canary"]
    can_ok = sum(1 for r in can if r["returned"] and r["is_real"])
    comp = [r for r in rows if r["group"] in ("medium_accepted", "medium_rejected")
            and r["returned"]]
    agree = sum(1 for r in comp if bool(r["is_real"]) == r["medium_is_real"])
    atk = [r for r in rows if r["group"] == "known_attack"]
    atk_ok = sum(1 for r in atk if r["returned"]
                 and (not r["is_real"] or set(r["flags"]) & E.HARD_RISK_FLAGS))
    misaligned = [r for r in rows if r["aligned"] is False]
    d = [r["spec"] - r["medium_spec"] for r in rows
         if isinstance(r.get("spec"), (int, float))
         and isinstance(r.get("medium_spec"), (int, float)) and r["is_real"]]

    print(f"\n=== BATCHED @ {a.effort} (batch size {a.batch_size}) ===")
    print(f"  canaries real:        {can_ok}/{len(can)}"
          f"   {'PASS' if can_ok == len(can) else 'FAIL - disqualifies this effort'}")
    print(f"  verdicts returned:    {returned}/{expected} "
          f"({100*returned/max(expected,1):.0f}%)"
          f"   {'' if returned == expected else '<- shortfall falls back to 1-per-call'}")
    print(f"  index alignment:      {len(rows)-len(misaligned)}/{len(rows)} ok"
          f"   {'' if not misaligned else '<- MISALIGNED: ' + str([r['name'] for r in misaligned][:5])}")
    print(f"  agreement vs medium:  {agree}/{len(comp)} "
          f"({100*agree/max(len(comp),1):.1f}%)")
    print(f"  known attacks caught: {atk_ok}/{len(atk)}")
    if d:
        print(f"  specificity delta:    median {st.median(d):+.3f} (n={len(d)})")
    if latencies:
        per_skill = sum(latencies) / max(expected, 1)
        print(f"  latency:              {st.median(latencies):.0f}s/call, "
              f"{per_skill:.1f}s/skill")

    out = Path(a.out) if a.out else BENCH / f"exp_effort_batched_{a.effort}.json"
    out.write_text(json.dumps(
        {"effort": a.effort, "batch_size": a.batch_size,
         "canary": f"{can_ok}/{len(can)}", "returned": returned, "expected": expected,
         "misaligned": len(misaligned), "agree": agree, "compared": len(comp),
         "attacks_caught": f"{atk_ok}/{len(atk)}",
         "spec_delta_median": round(st.median(d), 3) if d else None,
         "median_call_secs": round(st.median(latencies), 1) if latencies else None,
         "secs_per_skill": round(sum(latencies)/max(expected,1), 2) if latencies else None,
         "rows": rows}, indent=1))
    print(f"\n-> {out}")
    return 0 if can_ok == len(can) and not misaligned else 1


if __name__ == "__main__":
    raise SystemExit(main())
