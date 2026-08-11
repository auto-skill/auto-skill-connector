#!/usr/bin/env python3
"""Re-judge included skills whose summary/triggers came back in the wrong language.

Batched judging introduced cross-block language bleed: a judge call whose batch
contained Chinese or Japanese skills sometimes wrote CJK summaries and triggers
for the *English* skills in the same call (audit: 24 confirmed rows, clustered
in batches 22-26). Retrieval queries the corpus in English, so those skills were
effectively invisible — judged real, stored, packaged, and unfindable.

This re-runs each affected skill through a SINGLE-skill sealed Luna call (no
batch, so nothing to bleed from) using the prompt that now pins output language
to English, then propagates the corrected verdict everywhere it lives:

    enrichments row -> combined-batch row -> package provenance
    (skill_packages.manifest_json + skill_package_sources.provenance_json)

The package_hash does not change: package identity is entrypoint+files, and the
content is untouched. Only judge metadata moves.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
os.environ.setdefault("AUTOSKILL_LUNA_BATCH", "0")   # single calls only

import run2_enrich as E  # noqa: E402

CJK = re.compile(r"[぀-ヿ一-鿿가-힯]")


def cjk_share(t: str) -> float:
    t = t or ""
    return len(CJK.findall(t)) / max(len(t), 1)


def main() -> int:
    rows = json.loads((BENCH / "language_mismatch_rows.json").read_text())
    cache = {}
    for f in glob.glob(str(BENCH / "run2_fetch_b*.json")):
        try:
            cache.update(json.loads(Path(f).read_text()))
        except Exception:
            pass

    con = E.db()
    prompt = (BENCH / "enrichment_prompt_v2.md").read_text(encoding="utf-8")
    fixed = failed = 0
    corrected: dict[str, dict] = {}

    for row in rows:
        fe = cache.get(row["skill_id"])
        if not fe or fe.get("status") != "ok":
            failed += 1
            continue
        block, nh, truncated = E.build_judge_input({"id": row["skill_id"]}, fe)
        res = E.call_luna(prompt + "\n\n" + block)
        out = E.parse_judge_json(res.get("text") or "")
        if not out or cjk_share((out.get("summary") or "") +
                                " ".join(out.get("triggers") or [])) > 0.05:
            failed += 1
            print(f"  FAIL {row['name']}: no clean verdict", flush=True)
            continue
        out, _ = E.validate_output(out, E.file_paths(fe))
        out["truncated_input"] = truncated
        E.record_enrichment(con, nh, row["skill_id"], row.get("url"), "primary",
                            E.LUNA_SNAPSHOT, out,
                            res.get("tokens_in", 0), res.get("tokens_out", 0), "ok")
        corrected[row["norm_hash"]] = out
        fixed += 1
        print(f"  ok   {row['name']}: {str(out.get('summary'))[:60]}", flush=True)
    con.close()

    # propagate into combined batch rows
    for f in sorted(glob.glob(str(BENCH / "run2_combined_b*.json"))):
        d = json.loads(Path(f).read_text())
        changed = False
        for r in d.get("rows", []):
            out = corrected.get(r.get("norm_hash"))
            if out and r.get("label") == "included":
                r["primary"] = {**(r.get("primary") or {}),
                                **{k: out.get(k) for k in
                                   ("summary", "triggers", "specificity", "confidence",
                                    "vendor_convention", "risk_flags")}}
                changed = True
        if changed:
            tmp = Path(f + ".tmp")
            tmp.write_text(json.dumps(d, indent=1))
            os.replace(tmp, f)

    # propagate into package provenance
    pcon = sqlite3.connect(E.BACKEND / "corpus_v0_work.sqlite")
    upd = 0
    for ph, mj in list(pcon.execute(
            "select package_hash, manifest_json from skill_packages")):
        try:
            m = json.loads(mj)
        except Exception:
            continue
        nh = (m.get("provenance") or {}).get("norm_hash")
        out = corrected.get(nh)
        if not out:
            continue
        m["provenance"].update({"summary": out.get("summary"),
                                "triggers": out.get("triggers"),
                                "specificity": out.get("specificity"),
                                "language_fix": "v2.1-single-rejudge"})
        pcon.execute("update skill_packages set manifest_json=? where package_hash=?",
                     (json.dumps(m), ph))
        pcon.execute("update skill_package_sources set provenance_json=? where package_hash=?",
                     (json.dumps(m["provenance"]), ph))
        upd += 1
    pcon.commit()
    pcon.close()
    print(f"\nre-judged ok={fixed} failed={failed}; packages updated={upd}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
