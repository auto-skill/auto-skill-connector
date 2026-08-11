#!/usr/bin/env python3
"""Select the discriminative subset: keep only tasks the model FAILS unaided.

A task the subject already passes cannot show whether a skill helped -- round 1
put haiku at ceiling on 40 general tasks and every arm tied, which measured the
task set, not the corpus. This probes each candidate at baseline (no skill) and
keeps the failures.

Selecting on baseline failure is legitimate and must be disclosed: it makes the
set discriminative WITHOUT making it best-case, because selection never looks at
which skills exist in either corpus.
"""
import json, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from exp_corpus_ab import ask, grade

BENCH = Path(__file__).resolve().parent
tasks = json.loads((BENCH / "exp_tasks_hard.json").read_text())["tasks"]
print(f"probing {len(tasks)} candidates at baseline...", flush=True)

def probe(t):
    ok = grade(ask(t["prompt"], None), t)
    print(f"  {t['id']:16} baseline={'PASS' if ok else 'FAIL'}", flush=True)
    return t, ok

with ThreadPoolExecutor(max_workers=4) as ex:
    res = list(ex.map(probe, tasks))
hard = [t for t, ok in res if not ok]
print(f"\nbaseline failures (the discriminative set): {len(hard)}/{len(tasks)}")
(BENCH / "exp_tasks_hard_selected.json").write_text(
    json.dumps({"note": "selected by baseline failure only", "tasks": hard}, indent=1))
print(f"-> exp_tasks_hard_selected.json")
