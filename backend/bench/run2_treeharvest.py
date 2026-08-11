#!/usr/bin/env python3
"""Harvest git blob shas for every skill file, one API call per REPO.

Why this exists (measured): a random sample of 6,951 fetched sightings contained
13.3% exact-duplicate content — the most-forked skills appeared 31 times in the
sample alone. Birthday math puts the effective unique corpus at ~26-80k skills
against 517k sightings. The pipeline's scaling cost was the per-sighting fetch
(~1.5 quota calls each = ~155 quota-hours for the corpus); the judging cost only
ever scaled with UNIQUE content.

One `git/trees/HEAD?recursive=1` per repo (~70k repos ≈ 14 quota-hours total)
yields the blob sha of every skill file. Identical blob sha == identical bytes,
so a sighting whose sha maps to already-judged content can inherit that verdict
with zero fetch and zero Luna call — and unjudged work can be batched one
representative per sha instead of once per sighting.

Records into enrichment_v1.db:

  repo_trees(repo, path, sha, mode, size, harvested_at)
      -- SKILL.md files and every file in their skill directories
  repo_tree_meta(repo, status, files, skills, harvested_at)
      -- per-repo outcome: ok | truncated | gone | error:<class>

Safety: mode 120000 (symlink) is recorded verbatim — downstream uses it as the
AUTHORITATIVE symlink signal (stronger than the size-mismatch heuristic).
Honors the fleet STOP file and the gh interlock. Checkpointed and resumable.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
DB = BACKEND / "enrichment_v1.db"
STOP = Path("/srv/mobile-codex/autoskill-daemon/STOP")
# Smooth pacing beats burst-and-penalty. Observed: core quota ~99% UNUSED while
# throughput sat at ~850 repos/hr, because bursts tripped GitHub's SECONDARY
# limiter and each 403 cost a 120s penalty sleep. A steady inter-request gap
# stays under the burst detector and sustains ~3x the rate from the same quota.
PACE_SECONDS = float(os.environ.get("AUTOSKILL_HARVEST_PACE", "0.7"))
GH_BUSY = Path("/srv/mobile-codex/autoskill-daemon/state/gh_fetch_busy")

sys.path.insert(0, str(BENCH))
from run2_enrich import github_token, gh_trip_cooldown, gh_wait_cooldown  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def api(url: str, tok: str) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {tok}", "User-Agent": "autoskill-treeharvest",
        "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as fh:
            return fh.status, json.load(fh)
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code, None
    except Exception:
        return 0, None


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS repo_trees (
      repo TEXT, path TEXT, sha TEXT, mode TEXT, size INTEGER, harvested_at TEXT,
      PRIMARY KEY (repo, path));
    CREATE INDEX IF NOT EXISTS repo_trees_sha ON repo_trees(sha);
    CREATE TABLE IF NOT EXISTS repo_tree_meta (
      repo TEXT PRIMARY KEY, status TEXT, files INTEGER, skills INTEGER,
      harvested_at TEXT);
    """)
    con.commit()


def skill_rows(tree: list[dict]) -> list[tuple[str, str, str, int]]:
    """SKILL.md entries plus every blob inside their skill directories."""
    paths = [e for e in tree if e.get("type") == "blob"]
    skill_dirs = {str(Path(e["path"]).parent) + "/"
                  for e in paths if e["path"].endswith("SKILL.md")}
    out = []
    for e in paths:
        p = e["path"]
        # Root LICENSE files ride along free: license.spdx was 0.2% filled
        # because detection only ever saw skill-dir files. Recording the repo's
        # license blob makes corpus-wide SPDX backfill a pure local join.
        if (p.endswith("SKILL.md") or any(p.startswith(d) for d in skill_dirs)
                or ("/" not in p and p.upper().startswith(("LICENSE", "COPYING")))):
            out.append((p, e.get("sha") or "", e.get("mode") or "",
                        int(e.get("size") or 0)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=1200,
                    help="max repo tree calls this run")
    ap.add_argument("--summary-counts", action="store_true",
                    help="run full-table progress counts after harvesting")
    args = ap.parse_args()
    tok = github_token()

    con = sqlite3.connect(DB, timeout=300)
    con.execute("PRAGMA journal_mode=WAL")
    # Commit per repo, not per 50: a long write transaction here is what
    # starved the judging run's verdict writes into a crash.
    con.execute("PRAGMA busy_timeout=300000")
    con.execute("PRAGMA synchronous=NORMAL")
    ensure_tables(con)

    done = {r[0] for r in con.execute("select repo from repo_tree_meta")}
    repos = [r[0] for r in con.execute(
        "select distinct substr(external_id,1,instr(external_id,'::')-1)"
        " from sightings where external_id like '%::%'") if r[0] and r[0] not in done]
    # Coverage audit: 5,075 repos from the corpus-v0 scrape (71,788 known skill
    # files, 134 repos with 100+ stars) never produced a sighting, so a queue
    # built on sightings alone would never census them. Zero search cost to add.
    try:
        import json as _json
        wcon = sqlite3.connect(f"file:{BACKEND / 'corpus_v0_work.sqlite'}?mode=ro",
                               uri=True)
        seen = set(repos) | done
        for (raw,) in wcon.execute(
                "select raw from skills where source='github_skill_file'"):
            try:
                pr = (_json.loads(raw or "{}").get("parent_repo") or "").strip()
            except Exception:
                continue
            if pr and pr not in seen:
                seen.add(pr)
                repos.append(pr)
        wcon.close()
    except Exception:
        pass
    gh_wait_cooldown("(treeharvest)")
    print(f"treeharvest: {len(done)} done, {len(repos)} remaining, budget {args.budget}",
          flush=True)

    used = ok = 0
    for repo in repos:
        if used >= args.budget:
            break
        if STOP.exists():
            print("STOP present, exiting", flush=True)
            break
        # yield to enrichment's fetch stage (bounded; stale locks self-clear)
        waited = 0
        while GH_BUSY.exists() and waited < 600:
            try:
                owner = int(GH_BUSY.read_text().strip() or 0)
                if owner and not Path(f"/proc/{owner}").exists():
                    GH_BUSY.unlink(missing_ok=True)
                    break
            except Exception:
                pass
            time.sleep(20)
            waited += 20

        time.sleep(PACE_SECONDS)
        st, body = api(f"https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1", tok)
        used += 1
        if st == 200 and isinstance(body, dict):
            rows = skill_rows(body.get("tree") or [])
            status = "truncated" if body.get("truncated") else "ok"
            con.executemany(
                "insert or replace into repo_trees values (?,?,?,?,?,?)",
                [(repo, p, sha, mode, size, now()) for p, sha, mode, size in rows])
            con.execute("insert or replace into repo_tree_meta values (?,?,?,?,?)",
                        (repo, status, len(rows),
                         sum(1 for p, *_ in rows if p.endswith("SKILL.md")), now()))
            ok += 1
        elif st in (404, 451):     # gone / dmca — a real corpus fact
            con.execute("insert or replace into repo_tree_meta values (?,?,?,?,?)",
                        (repo, "gone", 0, 0, now()))
        elif st == 403:            # SECONDARY limit -- token-wide, shared backoff
            # 45s was far too short: measured, the block only lifts after ~300s
            # of total quiet, so retrying sooner just renews it for everyone.
            # End this slice immediately. The loop's next invocation honors the
            # shared cooldown before issuing another request; continuing through
            # the remaining budget turns one refusal into a sustained outage for
            # the higher-priority enrichment fetcher.
            gh_trip_cooldown()
            print(f"  403 at {used} calls — secondary limit, standing down", flush=True)
            break
        else:
            con.execute("insert or replace into repo_tree_meta values (?,?,?,?,?)",
                        (repo, f"error:{st}", 0, 0, now()))
        if used % 10 == 0:
            con.commit()
            print(f"  {used}/{args.budget} calls, {ok} ok", flush=True)
    con.commit()
    if args.summary_counts:
        tot = con.execute("select count(*) from repo_tree_meta").fetchone()[0]
        shas = con.execute("select count(distinct sha) from repo_trees"
                           " where path like '%SKILL.md'").fetchone()[0]
        print(f"treeharvest done: +{ok} repos this run, {tot} total, "
              f"{shas} distinct SKILL.md blob shas", flush=True)
    else:
        print(f"treeharvest done: +{ok} repos this run (summary deferred)",
              flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
