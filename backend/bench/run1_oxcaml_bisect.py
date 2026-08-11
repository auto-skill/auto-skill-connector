#!/usr/bin/env python3
"""Ingestion run 1 / Phase 2 — Oxcaml bisect (diagnosis only, no fixes).

Answers two questions against the FROZEN v0 corpus plus live GitHub metadata:

  (a) Does any pipeline row anywhere reference `oxcaml/oxcaml`?
  (b) Did the `.claude/skills` deep sweeps ever run to completion?

(b) normally reads `deep_sweep.last_sweep_at` out of
`backend/skills_library/crawl_state.json`, but that file lives in the package
directory that only exists on the production host, and this run is forbidden
from touching prod. So (b) is answered by a *proxy*: sample the live GitHub
code-search result set for the two sweep queries and measure what fraction of
those (repo, path) pairs the frozen corpus already knows. The proxy nature of
this evidence is carried through into the verdict.

Read-only. Writes nothing except its own JSON result file.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
FROZEN = BACKEND / "corpus_v0_frozen" / "corpus_v0.sqlite"
OUT = BENCH / "run1_bisect_evidence.json"

TARGET_REPO = "oxcaml/oxcaml"
TARGET_PATH = ".claude/skills/address-review/SKILL.md"

SWEEP_QUERIES = [
    "path:.claude/skills filename:SKILL.md",
    "path:.claude/skills",
]

# GitHub code search allows 10 req/min. Stay under it with a hard floor.
CODE_SEARCH_MIN_INTERVAL = 7.0
_last_call = [0.0]


def github_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    if hosts.exists():
        m = re.search(r"oauth_token:\s*(\S+)", hosts.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    raise SystemExit("no GitHub token available (env GITHUB_TOKEN or gh hosts.yml)")


def api(url: str, token: str, *, code_search: bool = False) -> tuple[int, dict]:
    if code_search:
        wait = CODE_SEARCH_MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "autoskill-run1-bisect",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as f:
                body = json.loads(f.read() or b"{}")
                if code_search:
                    _last_call[0] = time.time()
                return f.status, body
        except urllib.error.HTTPError as e:
            if code_search:
                _last_call[0] = time.time()
            if e.code in (403, 429) and attempt < 3:
                time.sleep(20 * (attempt + 1))
                continue
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}
        except Exception as e:  # transient network
            if attempt == 3:
                return -1, {"error": str(e)}
            time.sleep(5)
    return -1, {}


def code_search(q: str, token: str, page: int = 1, per_page: int = 100) -> tuple[int, dict]:
    url = (
        "https://api.github.com/search/code?q="
        + urllib.parse.quote(q, safe="")
        + f"&per_page={per_page}&page={page}"
    )
    return api(url, token, code_search=True)


# ---------------------------------------------------------------- corpus side


def load_corpus_index() -> tuple[set[tuple[str, str]], set[str], dict]:
    """(parent_repo, path) pairs and the repo set from the frozen corpus."""
    if not FROZEN.exists():
        raise SystemExit(f"frozen corpus missing: {FROZEN}")
    con = sqlite3.connect(f"file:{FROZEN}?mode=ro", uri=True)
    cur = con.cursor()
    pairs: set[tuple[str, str]] = set()
    repos: set[str] = set()
    stats = {"rows_scanned": 0, "rows_with_repo_and_path": 0}
    cur.execute("select raw from skills where raw is not null and raw != '{}'")
    for (raw,) in cur:
        stats["rows_scanned"] += 1
        try:
            d = json.loads(raw)
        except Exception:
            continue
        repo = (d.get("parent_repo") or "").strip()
        path = (d.get("path") or "").strip()
        if repo:
            repos.add(repo.casefold())
        if repo and path:
            stats["rows_with_repo_and_path"] += 1
            pairs.add((repo.casefold(), path.casefold()))
    con.close()
    return pairs, repos, stats


def scan_for_oxcaml() -> dict:
    """Question (a): any reference to oxcaml/oxcaml in any non-PII table."""
    con = sqlite3.connect(f"file:{FROZEN}?mode=ro", uri=True)
    cur = con.cursor()
    pii = {
        "users", "cli_tokens", "oauth_identities", "oauth_clients", "mcp_auth_codes",
        "stripe_subscriptions", "stripe_webhook_events", "route_events", "route_usage",
        "route_outcome_metrics", "route_survey_state", "admin_audit_log", "org_audit_log",
        "org_members", "orgs", "favorites", "installs", "complimentary_entitlements",
        "routing_preferences", "measurement_mode_settings", "org_skill_policies",
        "_litestream_lock", "_litestream_seq",
    }
    cur.execute("select name from sqlite_master where type='table'")
    tables = [r[0] for r in cur.fetchall()]
    scanned, hits = [], []
    for t in tables:
        if t in pii or t.startswith("sqlite_") or "_fts" in t:
            continue
        cur.execute(f'PRAGMA table_info("{t}")')
        cols = [
            r[1] for r in cur.fetchall()
            if (r[2] or "").upper() in ("TEXT", "") or "CHAR" in (r[2] or "").upper()
        ]
        if not cols:
            continue
        where = " OR ".join(f'"{c}" LIKE ?' for c in cols)
        try:
            cur.execute(f'select * from "{t}" where {where} limit 50', ["%oxcaml%"] * len(cols))
        except sqlite3.Error:
            continue
        names = [d[0] for d in cur.description]
        found = cur.fetchall()
        scanned.append(t)
        for row in found:
            rec = dict(zip(names, [str(v)[:240] for v in row]))
            blob = json.dumps(rec).casefold()
            rec["_is_target_repo"] = TARGET_REPO in blob
            hits.append({"table": t, "row": rec})
    con.close()
    return {
        "tables_scanned": scanned,
        "substring_hits": hits,
        "target_repo_rows": [h for h in hits if h["row"].get("_is_target_repo")],
    }


# ------------------------------------------------------------------ live side


def sample_sweep_coverage(token: str, pairs: set, repos: set, budget_requests: int = 12) -> dict:
    """Sample live code-search hits per sweep query; measure corpus coverage."""
    out = {"queries": [], "budget_requests": budget_requests}
    spent = 0
    for q in SWEEP_QUERIES:
        entry: dict = {"query": q, "total_count": None, "sampled": 0, "in_corpus": 0,
                       "repo_in_corpus": 0, "pages": [], "misses_sample": []}
        status, body = code_search(q, token, page=1, per_page=100)
        spent += 1
        if status != 200:
            entry["error"] = {"status": status, "body": str(body)[:300]}
            out["queries"].append(entry)
            continue
        entry["total_count"] = body.get("total_count")
        pages = [body]
        # a couple more pages for a wider sample, budget permitting
        for page in (2, 3, 4):
            if spent >= budget_requests // len(SWEEP_QUERIES) * (SWEEP_QUERIES.index(q) + 1):
                break
            st, bd = code_search(q, token, page=page, per_page=100)
            spent += 1
            if st != 200:
                entry["pages"].append({"page": page, "status": st})
                break
            pages.append(bd)
        for bd in pages:
            for item in bd.get("items", []):
                repo = (item.get("repository", {}).get("full_name") or "").casefold()
                path = (item.get("path") or "").casefold()
                entry["sampled"] += 1
                if (repo, path) in pairs:
                    entry["in_corpus"] += 1
                else:
                    if repo in repos:
                        entry["repo_in_corpus"] += 1
                    if len(entry["misses_sample"]) < 25:
                        entry["misses_sample"].append(
                            {"repo": item.get("repository", {}).get("full_name"),
                             "path": item.get("path"),
                             "repo_known_to_corpus": repo in repos}
                        )
        entry["coverage_pct"] = (
            round(100.0 * entry["in_corpus"] / entry["sampled"], 1) if entry["sampled"] else None
        )
        out["queries"].append(entry)
    out["requests_spent"] = spent
    return out


def probe_target(token: str) -> dict:
    """Is the target file (i) real, and (ii) visible to GitHub code search?"""
    res: dict = {"repo": TARGET_REPO, "path": TARGET_PATH}
    st, body = api(
        f"https://api.github.com/repos/{TARGET_REPO}/contents/{urllib.parse.quote(TARGET_PATH)}",
        token,
    )
    res["contents_api"] = {"status": st}
    if st == 200:
        res["contents_api"].update(
            {"size": body.get("size"), "sha": body.get("sha"), "exists": True}
        )
    else:
        res["contents_api"]["exists"] = False

    st, body = api(f"https://api.github.com/repos/{TARGET_REPO}", token)
    if st == 200:
        res["repo_meta"] = {
            "stars": body.get("stargazers_count"),
            "size_kb": body.get("size"),
            "fork": body.get("fork"),
            "archived": body.get("archived"),
            "private": body.get("private"),
            "pushed_at": body.get("pushed_at"),
            "default_branch": body.get("default_branch"),
        }

    # Is it indexed by code search at all, scoped to the repo?
    for q in (
        f"repo:{TARGET_REPO} path:.claude/skills filename:SKILL.md",
        f"repo:{TARGET_REPO} path:.claude/skills",
        f"repo:{TARGET_REPO} filename:SKILL.md",
    ):
        st, body = code_search(q, token, per_page=100)
        res.setdefault("code_search", []).append(
            {
                "query": q,
                "status": st,
                "total_count": body.get("total_count") if st == 200 else None,
                "paths": [i.get("path") for i in body.get("items", [])][:20] if st == 200 else None,
                "error": None if st == 200 else str(body)[:200],
            }
        )
    return res


def main() -> int:
    token = github_token()
    print("loading frozen corpus index …", flush=True)
    pairs, repos, stats = load_corpus_index()
    print(f"  {stats['rows_scanned']} raw rows, {len(pairs)} (repo,path) pairs, {len(repos)} repos",
          flush=True)

    print("question (a): scanning for oxcaml …", flush=True)
    a = scan_for_oxcaml()
    print(f"  substring hits: {len(a['substring_hits'])}, target-repo rows: {len(a['target_repo_rows'])}",
          flush=True)

    print("probing target on GitHub …", flush=True)
    target = probe_target(token)

    print("question (b): sampling sweep coverage …", flush=True)
    b = sample_sweep_coverage(token, pairs, repos)
    for q in b["queries"]:
        print(f"  {q['query']!r}: total={q.get('total_count')} sampled={q['sampled']} "
              f"in_corpus={q['in_corpus']} ({q.get('coverage_pct')}%)", flush=True)

    result = {
        "run_id": "run1-20260802",
        "frozen_corpus": str(FROZEN),
        "corpus_index_stats": stats | {"distinct_pairs": len(pairs), "distinct_repos": len(repos)},
        "question_a_oxcaml_rows": a,
        "target_probe": target,
        "question_b_sweep_coverage": b,
        "crawl_state_available": False,
        "crawl_state_expected_path": "backend/skills_library/crawl_state.json (production host only)",
    }
    OUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
