#!/usr/bin/env python3
"""v1.1 benchmark suite: adversarially screened for sensitivity.

Round-1 finding (journal 2026-08-22): suite v1 was insensitive -- baseline 87%,
and 3 of 4 dev failures were checker-strictness artifacts, not knowledge gaps.
v1.1 fixes both failure modes, pre-registered BEFORE any policy tuning on it:

1. Equivalence-tolerant checkers: the generator must write a regex accepting
   every semantically-equivalent surface form (trailing slash, optional
   version suffix, whitespace, quoting), and it is validated against its own
   canonical answer.
2. Adversarial screening: each candidate is immediately answered by the
   BASELINE (no-skill) model. Kept tasks are ~70% baseline-failures (where
   uplift can appear) + ~30% baseline-passes (calibration; regressions must
   also be measurable). Screening uses only the fixed baseline arm, so no
   candidate policy information leaks into suite construction.
3. Long-tail seeding: domains sampled from LOW-prominence corpus skills --
   the tail where reference material genuinely decides the answer.

Output: tasks_v11_candidates.jsonl with per-task screen results; freezing to
tasks_frozen_v11.jsonl happens in a separate human-curated step, same as v1.
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
BACKEND = BENCH.parent
sys.path.insert(0, str(BENCH))
from run2_enrich import call_luna  # noqa: E402

DB = BACKEND / "skills_judged_v2.db"
OUT = HERE / "tasks_v11_candidates.jsonl"
TARGET_FAIL = 100     # baseline-failure tasks wanted
TARGET_PASS = 45      # calibration baseline-pass tasks wanted
SEED = 20260822

GEN_PROMPT = """You write benchmark tasks that test whether an AI assistant can
produce precise, verifiable technical answers WITHOUT running anything.

Domain context (topic inspiration only -- do NOT quote or depend on it):
<domain>
{domain}
</domain>

Write ONE HARD micro-task in this domain. Hard requirements:
- The correct answer is a stable, public, documented fact or exact form that
  a generalist would often NOT know from memory (obscure flag, niche config
  key, exact API path, tail-tool idiom) -- but a specialist reference makes
  near-certain.
- Machine-checkable with a regex. The regex MUST accept every semantically
  equivalent surface form: optional trailing slash, optional version suffix,
  flexible whitespace, single/double quotes, equivalent command spellings.
  A correct answer differing only in formatting must PASS.
- No code execution, no network, no repo-specific facts.

Reply with EXACTLY one JSON object, no prose:
{{"prompt": "<task, ending with: Answer with only the exact <thing>.>",
  "checker_regex": "<python re pattern accepting all equivalent forms>",
  "canonical_answer": "<one correct answer>",
  "equivalent_forms": ["<2-4 other acceptable surface forms>"],
  "domain": "<3-6 word label>"}}"""

ANSWER_PROMPT = """Answer the task below precisely and concisely.
Task:
{prompt}"""


def domains(n: int) -> list[str]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.execute("pragma busy_timeout=120000")
    rows = con.execute(
        "select category, name, capability_summary from skills"
        " where capability_summary != '' and prominence_score < 0.3"
        " order by random() limit ?", (n,)).fetchall()
    con.close()
    return [f"category={c or 'generic'}; skill={nm}; does: {s[:300]}"
            for c, nm, s in rows]


def main() -> int:
    random.seed(SEED)
    n_fail = n_pass = 0
    seen_prompts = set()
    if OUT.exists():
        for line in OUT.open():
            try:
                d = json.loads(line)
                seen_prompts.add(d["prompt"])
                if d["baseline_pass"]:
                    n_pass += 1
                else:
                    n_fail += 1
            except Exception:
                pass
    print(f"resuming: {n_fail} fail / {n_pass} pass candidates", flush=True)
    seeds = domains((TARGET_FAIL + TARGET_PASS) * 4)
    with OUT.open("a", encoding="utf-8") as fh:
        for i, dom in enumerate(seeds):
            if n_fail >= TARGET_FAIL and n_pass >= TARGET_PASS:
                break
            r = call_luna(GEN_PROMPT.format(domain=dom))
            text = (r.get("text") or "").strip()
            try:
                d = json.loads(text[text.index("{"): text.rindex("}") + 1])
                assert d.get("prompt") and d.get("checker_regex") and d.get("canonical_answer")
                assert d["prompt"] not in seen_prompts, "dup"
                cre = re.compile(d["checker_regex"])
                assert cre.search(d["canonical_answer"]), "checker rejects own answer"
                for eq in (d.get("equivalent_forms") or []):
                    assert cre.search(eq), f"checker rejects equivalent form: {eq[:40]}"
            except Exception as e:
                print(f"  [{i}] discard: {e}", flush=True)
                continue
            # Screening must run at the SAME setting as the model-under-test
            # (luna@none) or the kept-because-it-fails set won't transfer.
            b = call_luna(ANSWER_PROMPT.format(prompt=d["prompt"]), effort="none")
            answer = (b.get("text") or "").strip()
            if not answer and b.get("error"):
                print(f"  [{i}] baseline error, skip", flush=True)
                continue
            base_pass = bool(cre.search(answer))
            if base_pass and n_pass >= TARGET_PASS:
                continue
            if not base_pass and n_fail >= TARGET_FAIL:
                continue
            d["baseline_pass"] = base_pass
            d["baseline_answer"] = answer[:300]
            d["source_domain"] = dom[:200]
            d["gen_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            seen_prompts.add(d["prompt"])
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
            fh.flush()
            n_pass += base_pass
            n_fail += (not base_pass)
            print(f"  [{i}] kept base={'PASS' if base_pass else 'FAIL'}"
                  f" ({n_fail}F/{n_pass}P) {d.get('domain')}", flush=True)
    print(f"candidates: {n_fail} baseline-fail + {n_pass} baseline-pass -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
