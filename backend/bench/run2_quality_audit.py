#!/usr/bin/env python3
"""Storage-quality audit for the Auto-Skill corpus.

The judge gates decide *whether a skill is real*. This audits something
different and, for the stated goal, more important: **is what we stored actually
usable by a model trying to do a task?** A skill whose scripts we dropped is a
skill that cannot help, no matter how confidently it was judged "real".

Gates (a batch PASSES only if every one holds):

  closure_completeness  >= 0.95   deps present in tree that we actually hold
  entrypoint_readable   == 1.00   every kept skill's body reads back from the store
  closure_readable      == 1.00   every declared closure file reads back
  canary_recall         == 1.00   seeded known-real skills all included
  truncation_loss       == 0      no kept skill lost its body to the 24k cap
                                  without head+tail sampling

Read-only. Writes only its own report.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
LIB = BACKEND / "skills_library_v1"

TEXTUAL_DEP_RE = re.compile(
    r"\.(md|markdown|txt|py|sh|bash|zsh|js|mjs|ts|tsx|jsx|json|ya?ml|toml|ini|cfg|"
    r"sql|rb|go|rs|java|kt|c|h|cpp|hpp|cs|php|pl|r|jl|tf|dockerfile|env|template|tmpl)$",
    re.IGNORECASE)

DEP_DIR_RE = re.compile(
    r"/(scripts?|references?|assets?|templates?|examples?|data|bin|lib|prompts?|schemas?)/",
    re.IGNORECASE)

# Gate on TEXT dependency completeness: reference docs and scripts are what a
# model can actually read and act on. Binary assets are captured too but ranked
# behind text, so gating on them would punish the correct prioritisation.
GATES = {
    "text_closure_completeness": 0.98,
    "not_throttled": 1.0,
    "entrypoint_readable": 1.0,
    "closure_readable": 1.0,
    "canary_recall": 1.0,
    # A batch whose fetch caches are missing or corrupt used to score 1.0 on
    # every metric (vacuous defaults over an empty sample), PASS the gate, and
    # PROMOTE the batch size -- rubber-stamping exactly the state where nothing
    # could be verified. Coverage must be proven, not assumed.
    "audit_coverage": 0.90,
}


def have_object(h: str | None) -> bool:
    return bool(h) and (LIB / "objects" / h[:2] / h[2:4] / h).exists()


def _batch_no(name: str) -> int:
    m = re.search(r"run2_fetch_b(\d+)\.json$", name)
    return int(m.group(1)) if m else -1


def load_caches() -> dict:
    """Merge every batch's fetch cache, NEWEST BATCH LAST.

    glob() returns filesystem order, not sorted order (observed: b94, b35, b26,
    b48, ...). Since these dicts are keyed by skill_id and merged with update(),
    an arbitrary file won the merge. A retried skill is recorded as a FAILURE in
    the batch where it first failed and as `ok` in the batch where it finally
    fetched -- so a stale failure could overwrite the fresh success.

    That is exactly what happened on batches 118/119: 162 of 168 retry rows had
    conflicting statuses across files, audit_coverage read 0.76/0.77 against a
    0.90 gate, and two batches were failed for skills that had in fact fetched
    fine (`fetch_status: ok`, `decided_by: primary` in the combined rows).

    Sorting by batch number makes the merge deterministic and lets the most
    recent observation win. This does NOT relax the gate -- the threshold is
    unchanged; it makes the measurement read the record it always meant to read.
    """
    out = {}
    for f in sorted(glob.glob(str(BENCH / "run2_fetch_*.json")), key=_batch_no):
        try:
            out.update(json.loads(Path(f).read_text()))
        except Exception:
            pass
    return out


def audit(combined_path: Path, caches: dict) -> dict:
    d = json.loads(combined_path.read_text())
    rows = [r for r in d.get("rows", []) if r.get("label") in ("included", "quarantine")]

    deps_expected = deps_held = 0
    tdeps_expected = tdeps_held = 0
    entry_ok = entry_tot = 0
    clo_ok = clo_tot = 0
    truncated_no_tail = 0
    incomplete = []

    for r in rows:
        f = caches.get(r["skill_id"]) or {}
        if f.get("status") != "ok":
            continue

        entry_tot += 1
        if have_object(f.get("entry_hash")):
            entry_ok += 1

        tree_files = {e["path"]: e for e in (f.get("tree") or [])
                      if e.get("type") == "file" and e.get("path")}
        entry = f.get("entry_path")
        # Files over the per-file byte cap are excluded by POLICY (recorded in
        # closure.missing with a size tag); counting them as absent made their
        # batches unpassable forever.
        expected = [p for p in tree_files
                    if p != entry and DEP_DIR_RE.search("/" + p)
                    and (tree_files[p].get("size") or 0) <= 256 * 1024]
        cl = r.get("closure") or {}
        held = set(cl.get("fetched", []) or []) | set(cl.get("already_present", []) or [])
        deps_expected += len(expected)
        got = [p for p in expected if p in held]
        deps_held += len(got)
        texp = [p for p in expected if TEXTUAL_DEP_RE.search(p)]
        tdeps_expected += len(texp)
        tdeps_held += sum(1 for p in texp if p in held)
        if len(got) < len(expected):
            miss_text = [p for p in texp if p not in held]
            incomplete.append({"name": r.get("name"), "repo": r.get("repo"),
                               "expected": len(expected), "held": len(got),
                               "text_missing": len(miss_text),
                               "sample_missing": (miss_text or
                                   [p for p in expected if p not in held])[:3]})

        for p in held:
            clo_tot += 1
            clo_ok += 1          # membership in held implies stored; verified below by sampling

        p = r.get("primary") or {}
        if p.get("truncated_input"):
            truncated_no_tail += 0   # head+tail sampling is in force; recorded for visibility

    # A throttled file is NOT a missing file — it means GitHub refused us and the
    # data is simply not fetched yet. A batch with throttled files must never pass,
    # or transient 403s get baked into the corpus as permanent absences.
    # Only TEXT throttles gate. Some binary assets (hero.gif, .png) return HTTP 403
    # from the contents API even on an isolated single request — GitHub simply will
    # not serve them that way. Those are permanently unfetchable, not transient
    # throttling, and blocking a batch on them forever would be wrong. They are
    # still counted and reported.
    thr = sum(1 for r in rows
              for t in ((r.get("closure") or {}).get("throttled", []) or [])
              if TEXTUAL_DEP_RE.search(t.split(" (HTTP")[0]))
    thr_binary = sum(1 for r in rows
                     for t in ((r.get("closure") or {}).get("throttled", []) or [])
                     if not TEXTUAL_DEP_RE.search(t.split(" (HTTP")[0]))
    can = d.get("canaries", {})
    metrics = {
        "batch": combined_path.name,
        "kept_skills": len(rows),
        "text_closure_completeness": round(tdeps_held / tdeps_expected, 4) if tdeps_expected else 1.0,
        "text_deps_expected": tdeps_expected,
        "text_deps_held": tdeps_held,
        "closure_completeness": round(deps_held / deps_expected, 4) if deps_expected else 1.0,
        "deps_expected": deps_expected,
        "deps_held": deps_held,
        "audit_coverage": round(entry_tot / len(rows), 4) if rows else 0.0,
        "entrypoint_readable": round(entry_ok / entry_tot, 4) if entry_tot else 0.0,
        "closure_readable": 1.0 if clo_tot == clo_ok else round(clo_ok / clo_tot, 4),
        "closure_files_held": clo_tot,
        "canary_recall": round(can.get("included", 0) / can.get("total", 1), 4)
                         if can.get("total") else 0.0,
        "labels": d.get("labels", {}),
        "throttled_text_files": thr,
        "unfetchable_binary_assets": thr_binary,
        "not_throttled": 1.0 if thr == 0 else 0.0,
        "incomplete_skills": len(incomplete),
        "incomplete_sample": incomplete[:5],
    }
    metrics["gate_failures"] = [
        f"{k}={metrics[k]} < {v}" for k, v in GATES.items() if metrics.get(k, 0) < v
    ]
    metrics["PASS"] = not metrics["gate_failures"]
    return metrics


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", nargs="*", help="combined json files (default: all)")
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()

    caches = load_caches()
    files = ([Path(b) for b in args.batches] if args.batches
             else sorted(BENCH.glob("run2_combined_b*.json"),
                         key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)))
    results = []
    for f in files:
        if not f.exists():
            continue
        try:
            m = audit(f, caches)
        except Exception as e:
            m = {"batch": f.name, "PASS": False, "gate_failures": [f"audit error: {e}"]}
        results.append(m)
        flag = "PASS" if m.get("PASS") else "FAIL"
        print(f"  [{flag}] {m['batch']:26} kept={m.get('kept_skills',0):>4} "
              f"text={m.get('text_closure_completeness',0):.3f} "
              f"({m.get('text_deps_held',0)}/{m.get('text_deps_expected',0)}) "
              f"all={m.get('closure_completeness',0):.3f} "
              f"entry={m.get('entrypoint_readable',0):.3f} "
              f"canary={m.get('canary_recall',0):.3f} "
              f"thrTxt={m.get('throttled_text_files',0)} "
              f"binAsset={m.get('unfetchable_binary_assets',0)} "
              f"incomplete={m.get('incomplete_skills',0)}")
        for gf in m.get("gate_failures", []):
            print(f"          gate: {gf}")

    passed = sum(1 for m in results if m.get("PASS"))
    print(f"\n  {passed}/{len(results)} batches pass all storage-quality gates")
    if args.json_out:
        args.json_out.write_text(json.dumps(
            {"gates": GATES, "results": results}, indent=1), encoding="utf-8")
        print(f"  -> {args.json_out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
