#!/usr/bin/env python3
"""How low can Luna's reasoning effort go before the corpus gets worse?

The API reports the supported ladder as none / low / medium / high / xhigh / max.
Production has been on `medium` since run 2 started. Effort is the single biggest
lever on judging latency, so if `low` or `none` decides the same way, the whole
ingestion timeline shortens for free.

The trap this is built to avoid: comparing a cheap arm against stored `medium`
verdicts and reading every disagreement as damage. An LLM judge is not
deterministic -- some of that disagreement is just resampling noise, and without
knowing how much, a 10% disagreement rate is uninterpretable. So `medium` is
RE-RUN on the identical sample. Its disagreement with its own stored verdicts is
the noise floor, and a cheaper arm is only worse if it disagrees by more than
that.

The sample deliberately covers every decision the judge actually makes, because
a sample of only good skills would measure nothing about false accepts:

  canaries        hand-verified real. Must come back real -- this is a hard gate,
                  not a statistic. One miss halts the fleet in production.
  medium-accepted skills the current judge included -- measures false rejects
  medium-rejected skills the current judge called junk -- measures false accepts
  known attacks   the 5 confirmed malicious skills from the security scan --
                  measures whether cheap reasoning stops noticing injections

Reported per arm: agreement with stored medium, canary pass rate, junk rejection,
attack detection, specificity offset (same paired method as judge_calibration),
malformed-output rate, and latency. Latency is the entire point; every quality
number is the price being checked against it.

Read-only against the corpus. Judges run in the same sealed sandbox production
uses (env -i, read-only, --ephemeral, empty cwd). Skill content is UNTRUSTED and
is only ever passed as delimited data.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import statistics as st
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run2_enrich as E  # noqa: E402

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
WORK = BACKEND / "corpus_v0_work.sqlite"
DB = BACKEND / "enrichment_v1.db"
MEDIUM_SNAP = "gpt-5.6-luna@medium/codex-cli-0.144.6"


def call_luna_at(prompt_text: str, effort: str, timeout: int = 900) -> dict:
    """Same sealed invocation as production, with effort as the only variable."""
    jail = tempfile.mkdtemp(prefix=f"effort-{effort}-")
    t0 = time.time()
    try:
        p = subprocess.run(
            [E.CODEX_BIN, "exec", "--ephemeral", "--ignore-user-config",
             "--skip-git-repo-check", "-s", "read-only", "-C", jail,
             "-m", E.LUNA_MODEL, "-c", f'model_reasoning_effort="{effort}"', "-"],
            input=prompt_text,
            env={"HOME": "/home/sami", "PATH": "/usr/bin:/bin",
                 "CODEX_HOME": E.CODEX_HOME, "TERM": "dumb"},
            capture_output=True, text=True, timeout=timeout)
        return {"text": p.stdout or "", "rc": p.returncode,
                "err": (p.stderr or "")[-400:], "secs": time.time() - t0}
    except subprocess.TimeoutExpired:
        return {"text": "", "rc": -9, "err": "timeout", "secs": time.time() - t0}
    except Exception as exc:  # noqa: BLE001
        return {"text": "", "rc": -1, "err": f"{type(exc).__name__}: {exc}"[:200],
                "secs": time.time() - t0}
    finally:
        shutil.rmtree(jail, ignore_errors=True)


def build_sample(n_each: int, seed: int) -> list[dict]:
    """Canaries + medium-accepted + medium-rejected + confirmed attacks."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    verdicts: dict[str, dict] = {}
    for nh, oj in con.execute(
            "select norm_hash, output_json from enrichments"
            " where model_snapshot=? and judge_role='primary' and status='ok'",
            (MEDIUM_SNAP,)):
        try:
            verdicts[nh] = json.loads(oj)
        except Exception:
            continue
    con.close()

    # Fetched bytes live in the batch fetch caches, keyed by skill id.
    cache: dict[str, dict] = {}
    # All 70 caches total ~38 MB. The 5 confirmed attacks live in batches
    # 25-43, so a recent-only window silently drops the attack-detection
    # arm -- the one measuring whether cheap reasoning stops noticing
    # injections, which is exactly what must not be dropped silently.
    for f in sorted(BENCH.glob("run2_fetch_b*.json")):
        try:
            cache.update(json.loads(f.read_text()))
        except Exception:
            continue

    canaries, rows_by_nh = {}, {}
    for f in sorted(BENCH.glob("run2_combined_b*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        for r in d.get("rows", []):
            nh = r.get("norm_hash")
            if not nh:
                continue
            rows_by_nh[nh] = r
            if r.get("is_canary"):
                canaries[nh] = r

    attacks = set()
    scan = BENCH / "malicious_scan2.json"
    if scan.exists():
        try:
            for m in json.loads(scan.read_text()).get("confirmed", []):
                if m["name"] not in ("offensive-initial-access", "terminal-ops"):
                    attacks.add(m["name"])
        except Exception:
            pass

    rng = random.Random(seed)
    accepted = [nh for nh, v in verdicts.items()
                if v.get("is_real_skill") and nh in rows_by_nh and nh not in canaries]
    rejected = [nh for nh, v in verdicts.items()
                if not v.get("is_real_skill") and nh in rows_by_nh]
    rng.shuffle(accepted)
    rng.shuffle(rejected)

    picked: list[dict] = []

    def add(nh, group):
        r = rows_by_nh.get(nh)
        if not r:
            return False
        fe = cache.get(r.get("skill_id"))
        if not fe or fe.get("status") != "ok":
            return False
        try:
            block, _, _ = E.build_judge_input({"id": r["skill_id"]}, fe)
        except Exception:
            return False
        picked.append({"nh": nh, "group": group, "name": r.get("name"),
                       "block": block,
                       "medium_is_real": bool(verdicts.get(nh, {}).get("is_real_skill")),
                       "medium_spec": verdicts.get(nh, {}).get("specificity"),
                       "medium_flags": verdicts.get(nh, {}).get("risk_flags") or []})
        return True

    for nh in canaries:
        add(nh, "canary")
    got = 0
    for nh in accepted:
        if got >= n_each:
            break
        got += add(nh, "medium_accepted")
    got = 0
    for nh in rejected:
        if got >= n_each:
            break
        got += add(nh, "medium_rejected")
    for nh, r in rows_by_nh.items():
        if r.get("name") in attacks and not any(p["nh"] == nh for p in picked):
            add(nh, "known_attack")
    return picked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--efforts", default="none,low,medium")
    ap.add_argument("--n-each", type=int, default=30)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--out", default=str(BENCH / "exp_effort_sweep.json"))
    a = ap.parse_args()

    efforts = [e.strip() for e in a.efforts.split(",") if e.strip()]
    sample = build_sample(a.n_each, a.seed)
    groups = Counter(s["group"] for s in sample)
    print(f"sample: {len(sample)} skills  {dict(groups)}", flush=True)
    if not sample:
        print("no sample could be built (fetch caches missing?)")
        return 1

    prompt = (BENCH / "enrichment_prompt_v2.md").read_text()
    results: dict[str, list] = defaultdict(list)

    for eff in efforts:
        print(f"\n=== effort={eff} ===", flush=True)

        def one(s, _eff=eff):
            r = call_luna_at(prompt + "\n\n" + s["block"], _eff)
            out = E.parse_judge_json(r["text"]) if r["text"] else None
            return {**{k: s[k] for k in
                       ("nh", "group", "name", "medium_is_real", "medium_spec",
                        "medium_flags")},
                    "effort": _eff, "secs": round(r["secs"], 1), "rc": r["rc"],
                    "parsed": out is not None,
                    "is_real": (out or {}).get("is_real_skill"),
                    "spec": (out or {}).get("specificity"),
                    "flags": (out or {}).get("risk_flags") or [],
                    "err": r["err"][:120] if r["rc"] != 0 else ""}

        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            rows = list(ex.map(one, sample))
        results[eff] = rows
        ok = [r for r in rows if r["parsed"]]
        print(f"  parsed {len(ok)}/{len(rows)}   "
              f"median latency {st.median([r['secs'] for r in rows]):.1f}s", flush=True)

    print("\n" + "=" * 78)
    print(f"{'effort':8} {'parsed':>7} {'agree':>7} {'canary':>9} {'junk rej':>9} "
          f"{'attacks':>8} {'spec Δ':>7} {'med s':>7} {'tot min':>8}")
    print("-" * 78)
    summary = {}
    for eff in efforts:
        rows = results[eff]
        ok = [r for r in rows if r["parsed"]]
        comp = [r for r in ok if r["group"] in ("medium_accepted", "medium_rejected")]
        agree = sum(1 for r in comp if bool(r["is_real"]) == r["medium_is_real"])
        can = [r for r in rows if r["group"] == "canary"]
        can_ok = sum(1 for r in can if r["parsed"] and r["is_real"])
        rej = [r for r in ok if r["group"] == "medium_rejected"]
        rej_ok = sum(1 for r in rej if not r["is_real"])
        atk = [r for r in rows if r["group"] == "known_attack"]
        atk_ok = sum(1 for r in atk
                     if r["parsed"] and (not r["is_real"] or set(r["flags"]) & E.HARD_RISK_FLAGS))
        d = [r["spec"] - r["medium_spec"] for r in ok
             if isinstance(r.get("spec"), (int, float))
             and isinstance(r.get("medium_spec"), (int, float)) and r["is_real"]]
        secs = [r["secs"] for r in rows]
        summary[eff] = {
            "parsed": f"{len(ok)}/{len(rows)}",
            "agree_pct": round(100 * agree / max(len(comp), 1), 1),
            "canary": f"{can_ok}/{len(can)}",
            "junk_rejected": f"{rej_ok}/{len(rej)}",
            "attacks_caught": f"{atk_ok}/{len(atk)}",
            "spec_delta_median": round(st.median(d), 3) if d else None,
            "median_secs": round(st.median(secs), 1),
            "total_min": round(sum(secs) / 60, 1),
        }
        s = summary[eff]
        print(f"{eff:8} {s['parsed']:>7} {s['agree_pct']:>6.1f}% {s['canary']:>9} "
              f"{s['junk_rejected']:>9} {s['attacks_caught']:>8} "
              f"{str(s['spec_delta_median']):>7} {s['median_secs']:>7} "
              f"{s['total_min']:>8}")

    print("\n--- reading ---")
    if "medium" in summary:
        floor = 100 - summary["medium"]["agree_pct"]
        print(f"  medium re-run disagrees with its OWN stored verdicts on "
              f"{floor:.1f}% of skills.")
        print(f"  That is the noise floor. A cheaper arm is only genuinely worse if")
        print(f"  its disagreement clearly exceeds {floor:.1f}%.")
        base = summary["medium"]["median_secs"]
        for eff in efforts:
            if eff == "medium":
                continue
            sp = base / max(summary[eff]["median_secs"], 0.01)
            dis = 100 - summary[eff]["agree_pct"]
            print(f"  {eff:6}: {sp:.2f}x faster, disagreement {dis:.1f}% "
                  f"vs floor {floor:.1f}%  -> "
                  f"{'within noise' if dis <= floor + 3 else 'REAL degradation'}")
    print("\n  Canary column is a hard gate: anything below full marks disqualifies")
    print("  that effort regardless of how fast it is.")

    Path(a.out).write_text(json.dumps(
        {"sample_size": len(sample), "groups": dict(groups),
         "summary": summary, "rows": {k: v for k, v in results.items()}}, indent=1))
    print(f"\n-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
