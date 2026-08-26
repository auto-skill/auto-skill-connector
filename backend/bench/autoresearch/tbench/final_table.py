#!/usr/bin/env python3
"""Final marathon table for Discord: only trials from run dirs created AFTER
the installer fix count (earlier trials had no agent installed at all)."""
import glob
import json
import sys

CUTOFF = "2026-08-25__20"  # first fixed-installer run id prefix floor


def arm(root):
    out = {}
    for f in sorted(glob.glob(root + "/*/*/*/results.json")):
        run_id = f.split("/")[-4]
        if run_id < CUTOFF:
            continue
        try:
            d = json.load(open(f))
        except Exception:
            continue
        tid = f.split("/")[-3].split(".")[0]
        out[tid] = bool(d.get("is_resolved"))
    return out


R = "/srv/mobile-codex/sessions/autoskill_7e0dd3fa/tb-runs"
ra = arm(R + "/r5-baseline")
rb = arm(R + "/r5-autoskill")
rp = arm(R + "/r5-principles")
common = sorted(set(ra) & set(rb))
aw = sum(ra[t] for t in common)
bw = sum(rb[t] for t in common)
up = [t for t in common if rb[t] and not ra[t]]
dn = [t for t in common if ra[t] and not rb[t]]
pfix = [t for t in rp if rp[t] and not ra.get(t)]
msg = (f":trophy: MARATHON COMPLETE (fixed installer) — Terminal-Bench medium "
       f"(n={len(common)}): baseline {aw}/{len(common)} vs autoskill {bw}/{len(common)}. "
       f"fixes: {', '.join(up) or 'none'}; regressions: {', '.join(dn) or 'none'}. "
       f"Principle pilot: {sum(rp.values())}/{len(rp)} solved, "
       f"principle-only fixes: {', '.join(pfix) or 'none'}")
sys.stdout.write(msg + "\0")
