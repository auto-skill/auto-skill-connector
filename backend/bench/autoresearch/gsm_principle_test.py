#!/usr/bin/env python3
"""Do principle skills help REASONING (non-knowledge, non-agentic)?

GSM8K sample, weak Luna, paired arms: bare vs the corpus's best
decomposition/first-principles skill injected verbatim. Numeric exact-match
grading. Answers the third cell of the matrix (knowledge: +22 certified,
agentic: null, reasoning: ?).
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from run2_enrich import call_luna  # noqa: E402

N = 60
SEED = 20260826
PRINCIPLE_IDS = [  # from the shortlist: decomposition + first-principles
    ("first-principles-thinking", None),
    ("complex-task-breakdown", None),
]

BARE = "Solve this problem. End your reply with: ANSWER: <number>\n\n{q}"
WITH = """Reference method (use it to structure your work):
<reference>
{skills}
</reference>
Solve this problem using the method above. End your reply with: ANSWER: <number>

{q}"""


def gold(ans: str) -> str:
    return ans.split("####")[-1].strip().replace(",", "")


def pred(text: str) -> str:
    m = re.findall(r"ANSWER:\s*\$?(-?[\d,]+(?:\.\d+)?)", text or "")
    if m:
        return m[-1].replace(",", "")
    m = re.findall(r"(-?[\d,]+(?:\.\d+)?)", text or "")
    return m[-1].replace(",", "") if m else ""


def load_skills() -> str:
    import sqlite3
    B = HERE.parent.parent
    con = sqlite3.connect(f"file:{B}/skills_judged_v2.db?mode=ro", uri=True)
    con.execute("pragma busy_timeout=60000")
    parts = []
    for name, _ in PRINCIPLE_IDS:
        r = con.execute(
            "select canonical_id, name from skills where lower(name)=?"
            " order by quality_score desc limit 1", (name,)).fetchone()
        if r:
            p = B / "judged_library_v2" / "files" / f"{r[0]}.md"
            try:
                parts.append(f"## {r[1]}\n" + p.read_text(errors="replace")[:3000])
            except OSError:
                pass
    con.close()
    return "\n\n".join(parts)


def main() -> int:
    random.seed(SEED)
    rows = [json.loads(l) for l in open(HERE / "gsm8k_test.jsonl")]
    sample = random.sample(rows, N)
    skills = load_skills()
    print(f"skill block: {len(skills)} chars", flush=True)
    out = HERE / "gsm_results.jsonl"
    done = set()
    if out.exists():
        done = {json.loads(l)["i"] for l in out.open()}
    with out.open("a") as fh:
        for i, row in enumerate(sample):
            if i in done:
                continue
            g = gold(row["answer"])
            rb = call_luna(BARE.format(q=row["question"]), effort="none")
            rw = call_luna(WITH.format(skills=skills, q=row["question"]), effort="none")
            pb, pw = pred(rb.get("text")), pred(rw.get("text"))
            fh.write(json.dumps({"i": i, "gold": g, "bare": pb, "with": pw,
                                 "bare_ok": pb == g, "with_ok": pw == g}) + "\n")
            fh.flush()
            print(f"[{i}] gold={g} bare={pb}({pb==g}) with={pw}({pw==g})", flush=True)
    res = [json.loads(l) for l in out.open()]
    b = sum(r["bare_ok"] for r in res)
    w = sum(r["with_ok"] for r in res)
    fixes = sum(1 for r in res if r["with_ok"] and not r["bare_ok"])
    regr = sum(1 for r in res if r["bare_ok"] and not r["with_ok"])
    print(f"\nGSM8K n={len(res)}: bare {b} vs principle {w} (+{w-b}); fixes {fixes}, regr {regr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
