#!/usr/bin/env python3
"""Three-arm A/B: no skill vs OLD prod corpus vs NEW verified corpus.

Design choices and why:

* **Identical retrieval on both corpora.** The same BM25 scorer runs over the
  same field shape (name + description/summary + triggers). If retrieval code
  differed between arms, the experiment would measure the code, not the corpus.
* **Deterministic grading, no LLM judge.** Each task lists token groups a
  correct answer must contain (exact flags, function names, formats). This
  removes judge bias, is perfectly reproducible, and -- decisive here -- costs
  zero Luna quota, which the live ingestion needs and is currently rationing.
* **Tasks written before looking at either corpus.** No scouting for tasks that
  happen to have a matching skill; that is what makes a "best-case" number.
* **Read-only against both databases.** The ingestion fleet keeps writing
  corpus_v0_work.sqlite throughout; every connection here is mode=ro with a
  busy timeout, so the experiment cannot block or corrupt the running job.

Arms per task: baseline / prod-corpus / new-corpus. Same prompt, same model,
only the injected skill differs.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
WORK = BACKEND / "corpus_v0_work.sqlite"
LIB = BACKEND / "skills_library_v1"
CLAUDE = "/home/sami/.npm-global/bin/claude"

WORD = re.compile(r"[a-z0-9#+.-]{2,}")
STOP = set("the a an and or of to for in on with how do i my is are be use using what "
           "when where why can should give me exact command commands show it that this "
           "from at by as if then than into out over your you".split())


def toks(s: str) -> list[str]:
    return [w for w in WORD.findall((s or "").lower()) if w not in STOP]


class BM25:
    """One scorer, both corpora. k1/b are stock values; nothing tuned per-arm."""

    def __init__(self, docs: list[tuple[str, str]], k1=1.5, b=0.75):
        self.ids = [d[0] for d in docs]
        self.tok = [toks(d[1]) for d in docs]
        self.k1, self.b = k1, b
        self.len = [len(t) for t in self.tok]
        self.avg = sum(self.len) / max(len(self.len), 1)
        self.df: Counter = Counter()
        for t in self.tok:
            for w in set(t):
                self.df[w] += 1
        self.N = len(docs)
        self.tf = [Counter(t) for t in self.tok]

    def top(self, query: str, n=1):
        q = toks(query)
        scores = []
        for i in range(self.N):
            s = 0.0
            for w in q:
                f = self.tf[i].get(w, 0)
                if not f:
                    continue
                idf = math.log(1 + (self.N - self.df[w] + 0.5) / (self.df[w] + 0.5))
                s += idf * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * self.len[i] / max(self.avg, 1)))
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        return [(self.ids[i], sc) for sc, i in scores[:n]]


def load_prod() -> tuple[BM25, dict]:
    """OLD corpus: the unverified scrape the production router serves."""
    con = sqlite3.connect(f"file:{WORK}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=60000")
    docs, body = [], {}
    for sid, name, desc, raw in con.execute(
            "select id, name, description, raw from skills"
            " where source in ('github_skill_file','github')"
            "   and name is not null limit 300000"):
        text = f"{name} {desc or ''}"
        docs.append((sid, text))
        body[sid] = {"name": name, "text": desc or "", "raw": raw}
    con.close()
    return BM25(docs), body


def load_new() -> tuple[BM25, dict]:
    """NEW corpus: verified packages, indexed on judge summary + triggers."""
    con = sqlite3.connect(f"file:{WORK}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=60000")
    docs, body = [], {}
    for ph, mj in con.execute("select package_hash, manifest_json from skill_packages"):
        try:
            m = json.loads(mj)
        except Exception:
            continue
        p = m.get("provenance") or {}
        name = p.get("name") or ""
        summ = p.get("summary") or ""
        trg = " ".join(p.get("triggers") or [])
        if not (name or summ):
            continue
        docs.append((ph, f"{name} {summ} {trg}"))
        body[ph] = {"name": name, "text": summ, "manifest": m}
    con.close()
    return BM25(docs), body


def skill_text_prod(rec: dict) -> str:
    return f"{rec['name']}\n{rec['text']}"[:6000]


def skill_text_new(rec: dict) -> str:
    """Entrypoint bytes -- the actual skill, not just its summary."""
    m = rec["manifest"]
    ent = [f for f in m.get("files", []) if f["path"] == m.get("entrypoint")]
    if ent:
        h = ent[0]["raw_sha256"]
        p = LIB / "objects" / h[:2] / h[2:4] / h
        if p.exists():
            return p.read_bytes().decode("utf-8", "replace")[:6000]
    return f"{rec['name']}\n{rec['text']}"[:6000]


def ask(prompt: str, skill: str | None) -> str:
    sys_pre = ""
    if skill:
        sys_pre = ("You have been given a reference skill that may help. Use it if "
                   "relevant; ignore it if not.\n\n--- SKILL ---\n" + skill
                   + "\n--- END SKILL ---\n\n")
    try:
        r = subprocess.run([CLAUDE, "-p", "--model", "haiku"],
                           input=sys_pre + prompt, capture_output=True,
                           text=True, timeout=180)
        return r.stdout or ""
    except Exception as e:
        return f"__ERROR__ {e}"


def grade(answer: str, task: dict) -> bool:
    low = (answer or "").lower()
    for group in task["all_of"]:
        if not any(g.lower() in low for g in group):
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(BENCH / "exp_results.json"))
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    tasks = json.loads((BENCH / "exp_tasks.json").read_text())["tasks"]
    print(f"tasks: {len(tasks)}", flush=True)
    print("indexing prod corpus...", flush=True)
    prod_idx, prod_body = load_prod()
    print(f"  prod docs: {prod_idx.N:,}", flush=True)
    print("indexing new corpus...", flush=True)
    new_idx, new_body = load_new()
    print(f"  new docs:  {new_idx.N:,}", flush=True)

    def run_task(t):
        q = t["prompt"]
        ph = prod_idx.top(q, 1)
        nh = new_idx.top(q, 1)
        prod_skill = skill_text_prod(prod_body[ph[0][0]]) if ph else None
        new_skill = skill_text_new(new_body[nh[0][0]]) if nh else None
        out = {"id": t["id"],
               "prod_hit": (prod_body[ph[0][0]]["name"] if ph else None),
               "new_hit": (new_body[nh[0][0]]["name"] if nh else None)}
        for arm, sk in (("baseline", None), ("prod", prod_skill), ("new", new_skill)):
            a = ask(q, sk)
            out[arm] = grade(a, t)
            out[arm + "_len"] = len(a)
        print(f"  {t['id']:10} base={out['baseline']!s:5} "
              f"prod={out['prod']!s:5} new={out['new']!s:5}", flush=True)
        return out

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        rows = list(ex.map(run_task, tasks))

    n = len(rows)
    res = {a: sum(1 for r in rows if r[a]) for a in ("baseline", "prod", "new")}
    print("\n=== RESULTS ===")
    for a in ("baseline", "prod", "new"):
        print(f"  {a:9} {res[a]:>3}/{n}  ({100*res[a]/n:.0f}%)")
    Path(args.out).write_text(json.dumps({"n": n, "totals": res, "rows": rows}, indent=1))
    print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
