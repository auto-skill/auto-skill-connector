#!/usr/bin/env python3
"""Generate CANDIDATE benchmark tasks for the autoresearch pilot.

Candidates only: nothing here enters the certified suite until a human pass
curates them (leakage rule: a task whose only source of truth is skill text
gets cut) and the survivors are frozen with a sha256. Domains are sampled
from the corpus so the benchmark measures the corpus where it is dense, but
every task must be verifiable from public, stable knowledge -- the checker is
a machine assert, never a judgment call.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
BACKEND = BENCH.parent
sys.path.insert(0, str(BENCH))
from run2_enrich import call_luna  # sealed exec; reuses blackout handling  # noqa: E402

DB = BACKEND / "skills_judged_v2.db"
OUT = HERE / "tasks_candidates.jsonl"
N_TASKS = 60
SEED = 20260819

PROMPT = """You write benchmark tasks that test whether an AI assistant can
produce precise, verifiable technical answers WITHOUT running anything.

Domain context (for topic inspiration only -- do NOT quote or depend on it):
<domain>
{domain}
</domain>

Write ONE micro-task in this domain. Hard requirements:
- The correct answer is a stable, public, widely-documented fact or exact
  form (a flag, a config key, a command shape, a field name, an idiom).
- It must be checkable by a machine with a regex on the assistant's answer.
- It must NOT require executing code, network access, or reading any
  specific repository's files.
- Difficulty: a competent generalist gets it right ~half the time from
  memory; a specialist reference makes it near-certain.

Reply with EXACTLY one JSON object, no prose:
{{"prompt": "<the task, ending with: Answer with only the exact <thing>.>",
  "checker_regex": "<python re pattern the correct answer matches>",
  "canonical_answer": "<one correct answer string>",
  "domain": "<3-6 word domain label>"}}"""


def domains(n: int) -> list[str]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.execute("pragma busy_timeout=120000")
    rows = con.execute(
        "select category, name, capability_summary from skills"
        " where capability_summary != '' order by random() limit ?", (n,)).fetchall()
    con.close()
    return [f"category={c or 'generic'}; skill={n2}; does: {s[:300]}"
            for c, n2, s in rows]


def main() -> int:
    random.seed(SEED)
    have = 0
    if OUT.exists():
        have = sum(1 for _ in OUT.open())
    print(f"existing candidates: {have}", flush=True)
    seeds = domains(N_TASKS * 2)
    with OUT.open("a", encoding="utf-8") as fh:
        for i, dom in enumerate(seeds):
            if have >= N_TASKS:
                break
            r = call_luna(PROMPT.format(domain=dom))
            text = (r.get("text") or "").strip()
            try:
                start, end = text.index("{"), text.rindex("}") + 1
                d = json.loads(text[start:end])
                assert d.get("prompt") and d.get("checker_regex") and d.get("canonical_answer")
                import re as _re
                assert _re.search(d["checker_regex"], d["canonical_answer"]), "checker rejects own answer"
            except Exception as e:
                print(f"  [{i}] discard: {e}", flush=True)
                continue
            d["source_domain"] = dom[:200]
            d["gen_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
            fh.flush()
            have += 1
            print(f"  [{i}] kept ({have}/{N_TASKS}) {d.get('domain')}", flush=True)
    print(f"candidates: {have} -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
