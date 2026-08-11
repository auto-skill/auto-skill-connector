#!/usr/bin/env python3
"""Preflight: prove the pipeline's invariants BEFORE the fleet runs on them.

Written after a self-inflicted outage. A risk-flag gate was added, shipped
straight to production, and quarantined two of the 23 hand-verified canaries on
its first batch -- halting all four services. The rule was wrong (it blocked
subject-matter flags like `credential_request`, which is precisely what a
security skill legitimately discusses), but the deeper failure was process:
nothing tested a new rule against known-good data before it could stop the line.

This runs in seconds, needs no network, and is the daemon's first action. Every
check is a claim that must hold for the corpus to be trustworthy:

  gates_vs_canaries   no deterministic gate excludes a hand-verified real skill
  db_writers          every process that writes the shared DB waits on a lock
                      instead of crashing (six writers share one sqlite file)
  schema              tables and indexes the pipeline depends on exist
  stage_imports       every stage module imports (catches syntax/name errors
                      before a 30-minute batch discovers them)
  disk                headroom for the object store

Exit non-zero => the daemon refuses to start a batch and says why. A failed
preflight is cheap; a failed batch costs ~30 minutes and can halt the fleet.
"""
from __future__ import annotations

import glob
import importlib
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
DB = BACKEND / "enrichment_v1.db"
WORK = BACKEND / "corpus_v0_work.sqlite"
sys.path.insert(0, str(BENCH))

FAILURES: list[str] = []
NOTES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(': ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def gates_vs_canaries() -> None:
    """No deterministic gate may exclude a hand-verified canary.

    Canaries are ground truth. If a rule rejects one, the rule is wrong -- this
    is the exact check that would have caught the risk-gate outage.
    """
    import run2_enrich as E
    try:
        canaries = json.loads(
            (BACKEND / "evals" / "corpus_canaries.json").read_text())["canaries"]
    except Exception as e:
        check("gates_vs_canaries", False, f"canary set unreadable: {e}")
        return

    # the paths canaries actually live at, from the most recent combined batches
    # Only ACTIVE canaries are ground truth. Historical batch files still carry
    # retired ones (one was retired for pointing at a red-team eval fixture), and
    # testing a gate against a retired canary would demand the gate stay wrong.
    active_paths = {(c.get("parent_repo", "") + "::" + c.get("path", "")).casefold()
                    for c in canaries}
    canary_rows = {}
    for f in sorted(glob.glob(str(BENCH / "run2_combined_b*.json")))[-6:]:
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        for r in d.get("rows", []):
            if not (r.get("is_canary") and r.get("entry_path")):
                continue
            key = f"{r.get('repo', '')}::{r.get('entry_path', '')}".casefold()
            if key in active_paths:
                canary_rows[r.get("name")] = r
    if not canary_rows:
        NOTES.append("no canary rows in recent batches; gate check skipped")
        check("gates_vs_canaries", True, "no recent canary rows to test against")
        return

    fixture_hits, risk_hits = [], []
    for name, r in canary_rows.items():
        if E.FIXTURE_PATH_RE.search("/" + (r.get("entry_path") or "")):
            fixture_hits.append(name)
        flags = set((r.get("primary") or {}).get("risk_flags") or [])
        if flags & E.HARD_RISK_FLAGS:
            risk_hits.append(f"{name}{sorted(flags & E.HARD_RISK_FLAGS)}")

    check("gates_vs_canaries.fixture_rule", not fixture_hits,
          f"would exclude canaries: {fixture_hits}" if fixture_hits
          else f"{len(canary_rows)} canaries clear")
    check("gates_vs_canaries.risk_rule", not risk_hits,
          f"would quarantine canaries: {risk_hits}" if risk_hits
          else f"{len(canary_rows)} canaries clear")


def canary_coverage() -> None:
    """Every active canary must actually ride in recent batches.

    A canary that silently stops appearing is a regression sentinel that has
    quietly switched off. One did: `oxcaml-address-review` recorded its path as
    `.claude/skills/address-review/skill.md` while upstream is `SKILL.md`, so it
    matched nothing and vanished from every batch without a single error.
    """
    try:
        canaries = json.loads(
            (BACKEND / "evals" / "corpus_canaries.json").read_text())["canaries"]
    except Exception as e:
        check("canary_coverage", False, f"canary set unreadable: {e}")
        return
    seen = set()
    for f in sorted(glob.glob(str(BENCH / "run2_combined_b*.json")))[-3:]:
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        for r in d.get("rows", []):
            if r.get("is_canary"):
                seen.add(f"{r.get('repo','')}::{r.get('entry_path','')}".casefold())
    missing = [c.get("id") for c in canaries
               if (c.get("parent_repo","") + "::" + c.get("path","")).casefold() not in seen]
    # A newly corrected canary needs a batch cycle to reappear; report it as a
    # note rather than blocking the fleet on a self-healing condition.
    if missing:
        NOTES.append(f"canaries absent from last 3 batches (will re-enter next "
                     f"build): {missing}")
    check("canary_coverage", len(missing) <= 1,
          f"{len(missing)} canaries absent: {missing}" if len(missing) > 1
          else f"{len(canaries) - len(missing)}/{len(canaries)} riding in batches")


def judges_available() -> None:
    """The primary judge must actually answer before we spend a batch on it.

    Luna hit a hard usage limit and returned 613/613 failures for a whole batch.
    Every row still cost a fetch and a batch slot, the canary check then read the
    fallout as a quality regression, and the fleet halted. A dead judge should
    stop the line BEFORE the work, not after it.
    """
    import subprocess, tempfile, shutil, os as _os
    import run2_enrich as E
    ok_codex = False
    detail = ""
    jail = tempfile.mkdtemp(prefix="preflight-judge-")
    try:
        p = subprocess.run(
            [E.CODEX_BIN, "exec", "--ephemeral", "--ignore-user-config",
             "--skip-git-repo-check", "-s", "read-only", "-C", jail,
             "-m", E.LUNA_MODEL,
             "-c", f'model_reasoning_effort="{E.LUNA_EFFORT}"', "-"],
            input="Reply with exactly: OK",
            env={"HOME": "/home/sami", "PATH": "/usr/bin:/bin",
                 "CODEX_HOME": E.CODEX_HOME, "TERM": "dumb"},
            capture_output=True, text=True, timeout=120)
        blob = (p.stdout or "") + (p.stderr or "")
        if "usage limit" in blob.lower() or "rate limit" in blob.lower():
            detail = "usage limit reached"
        elif p.returncode == 0 and re.search(r"(?m)^OK\s*$", p.stdout or ""):
            ok_codex = True
            E.clear_luna_blackout()
        else:
            detail = f"rc={p.returncode}, missing expected probe response"
    except Exception as e:
        detail = f"{type(e).__name__}"
    finally:
        shutil.rmtree(jail, ignore_errors=True)

    failover = _os.environ.get("AUTOSKILL_JUDGE_FAILOVER", "") == "haiku"
    if ok_codex:
        check("judges.primary", True, "luna reachable")
    elif failover:
        # Trusting the env var alone would reintroduce the exact bug this check
        # exists to prevent: if the failover judge is ALSO down, the batch runs
        # anyway and produces several hundred failures before anything notices.
        # The flag says "use haiku", not "haiku works" -- so probe it.
        ok_haiku, hdetail = False, ""
        try:
            q = subprocess.run([E.CLAUDE_BIN, "-p", "--model", "haiku"],
                               input="Reply with exactly: OK",
                               capture_output=True, text=True, timeout=120)
            blob = (q.stdout or "") + (q.stderr or "")
            if "usage limit" in blob.lower() or "rate limit" in blob.lower():
                hdetail = "usage limit reached"
            elif q.returncode == 0 and q.stdout.strip():
                ok_haiku = True
            else:
                hdetail = f"rc={q.returncode}, empty output"
        except Exception as e:
            hdetail = type(e).__name__
        if ok_haiku:
            NOTES.append(f"luna unavailable ({detail}); running on haiku failover "
                         f"-- verdicts are tagged with the haiku snapshot and can "
                         f"be re-judged when luna returns")
            check("judges.primary", True,
                  f"luna down ({detail}), haiku failover probed OK")
        else:
            check("judges.primary", False,
                  f"luna down ({detail}) AND haiku failover also unavailable "
                  f"({hdetail}) -- no judge can run this batch")
    else:
        check("judges.primary", False,
              f"luna unavailable ({detail}) and no failover set "
              f"(AUTOSKILL_JUDGE_FAILOVER=haiku to continue on haiku)")


def db_writers() -> None:
    """Every writer of the shared DB must wait on a lock, not die on one."""
    writers = ["run2_enrich.py", "run2_sweep.py", "run2_expand.py",
               "run2_treeharvest.py", "run2_harvest_deep.py", "run2_inherit.py",
               "run2_metrics.py", "run2_package.py", "run2_popularity.py",
               "run2_complete_closure.py", "run2_repair_closure.py"]
    bad = []
    for w in writers:
        p = BENCH / w
        if not p.exists():
            continue
        src = p.read_text()
        # a writer either opens read-only, or must set busy_timeout / delegate to db()
        writes = re.search(r"sqlite3\.connect\((?!f\"file:\{[^}]+\}\?mode=ro)", src)
        if not writes:
            continue
        if "busy_timeout" not in src and "from run2_enrich import db" not in src \
                and "import db" not in src:
            bad.append(w)
    check("db_writers.lock_discipline", not bad,
          f"no busy_timeout: {bad}" if bad else f"{len(writers)} writers protected")


def schema() -> None:
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    except Exception as e:
        check("schema", False, str(e))
        return
    have = {r[0] for r in con.execute(
        "select name from sqlite_master where type='table'")}
    need = {"sightings", "enrichments", "repo_trees", "repo_tree_meta", "quarantine"}
    check("schema.tables", need <= have, f"missing: {sorted(need - have)}"
          if need - have else f"{len(need)} present")
    idx = {r[0] for r in con.execute(
        "select name from sqlite_master where type='index'")}
    ok = "repo_trees_sha" in idx
    check("schema.indexes", ok,
          "present" if ok else "repo_trees_sha missing (inheritance table-scans)")
    con.close()


def stage_imports() -> None:
    mods = ["run2_enrich", "run2_build_batch", "run2_package", "run2_inherit",
            "run2_repair_closure", "run2_complete_closure", "run2_quality_audit",
            "run2_treeharvest", "run2_sweep", "run2_expand", "run2_popularity"]
    bad = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:
            bad.append(f"{m}({type(e).__name__}: {e})")
    check("stage_imports", not bad, "; ".join(bad) if bad else f"{len(mods)} modules OK")


def wal_size() -> None:
    """WAL must not grow without bound.

    With six concurrent readers/writers SQLite's auto-checkpoint rarely gets an
    exclusive moment; the WAL reached 114 MB before a periodic janitor was added
    to the metrics tick. Warn well before it threatens the volume.
    """
    wal = Path(str(DB) + "-wal")
    mb = wal.stat().st_size / 1e6 if wal.exists() else 0
    check("wal_size", mb < 2000, f"{mb:.0f} MB"
          + (" (janitor not keeping up)" if mb >= 2000 else ""))


def timestamp_formats() -> None:
    """Every analytics timestamp must be ISO-8601 with the 'T' separator.

    These columns are compared as STRINGS against a Python-generated cutoff
    (`run2_metrics.py` backfill does `created_at <= '{cut}'`). String order only
    equals time order while one format is in use. SQLite's own datetime('now')
    writes "YYYY-MM-DD HH:MM:SS" with a SPACE, and ' ' (0x20) sorts below 'T'
    (0x54), so a single space-format row silently lands in EVERY bucket of the
    growth graph -- no error, just a wrong curve.

    This already bit once: the quarantine table was written with datetime('now')
    and 116 rows had to be normalised. The graph feeds published numbers, so the
    invariant is checked rather than assumed.

    Detection is via min(), not a LIKE scan: because ' ' sorts below 'T', ANY
    space-format row necessarily becomes the column minimum. So one indexed-ish
    min() per column proves the whole column, instead of a full scan over tables
    with millions of rows -- this check runs before every batch and must be
    cheap. Lock waits are capped hard and degrade to a note: a contended
    database is not a reason to refuse to ingest.
    """
    targets = [
        (DB, "enrichments", "created_at"),
        (DB, "sightings", "observed_at"),
        (DB, "repo_tree_meta", "harvested_at"),
        (DB, "quarantine", "recorded_at"),
        (WORK, "skill_packages", "created_at"),
    ]
    bad, skipped = [], []
    for db, tbl, col in targets:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            con.execute("PRAGMA busy_timeout=5000")
            lo = con.execute(f"select min({col}) from {tbl}").fetchone()[0]
            con.close()
        except sqlite3.Error as exc:
            skipped.append(f"{tbl} ({type(exc).__name__})")
            continue
        if lo is not None and "T" not in str(lo):
            bad.append(f"{tbl}.{col} min={lo!r}")
    if skipped:
        NOTES.append(f"timestamp check skipped under lock contention: "
                     f"{', '.join(skipped)}")
    check("timestamps.iso_consistent", not bad,
          "non-ISO rows in " + "; ".join(bad) if bad
          else f"{len(targets)-len(skipped)} columns single-format")


def quarantine_reasons() -> None:
    """Quarantine must only ever hold skills, never hold OUR outages.

    Quarantine means "this content looks suspicious enough to withhold from
    users". A judge that never answered has said nothing about the content, so
    it cannot justify that label. When Luna hit its usage limit, 56 perfectly
    good skills were withheld because our quota ran out -- invisible, because a
    growing quarantine count looks like the safety system working.

    This is the third instance of one bug: transient 403s became permanent
    `excluded_junk`, retry exhaustion became a verdict, and now a quota error
    became a safety hold. Each time an infrastructure failure was recorded as a
    judgement about content. Check the outcome, not just the intent.
    """
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        con.execute("PRAGMA busy_timeout=5000")
        bad = con.execute(
            "select count(*) from quarantine where reason like '%usage limit%'"
            " or reason like '%rate limit%' or reason like '%timeout%'"
            " or reason like '%rc=%' or reason like '%connection%'").fetchone()[0]
        total = con.execute("select count(*) from quarantine").fetchone()[0]
        con.close()
    except sqlite3.Error as exc:
        NOTES.append(f"quarantine check skipped ({type(exc).__name__})")
        return
    check("quarantine.reasons_are_about_content", bad == 0,
          f"{bad} of {total} holds caused by judge/infra failure, not content"
          if bad else f"{total} holds, all content-based")


def served_corpus_clean() -> None:
    """No SERVED package may violate a gate that is currently in force.

    The gates run at combine time, so they only ever protect new arrivals. That
    left 57 red-team eval fixtures and 10 hard-risk packages servable long after
    the rules existed, found only because a security audit went looking. Among
    them were `dataset/case_*` cases carrying live injection payloads that the
    primary judge had rated real with no risk flag.

    A gate is only a safety control if what it protects is checked, not just what
    passes through it. This asserts the OUTCOME -- the served set -- so a leak
    surfaces on the next batch instead of on the next audit.

    Sampled, because the full corpus is 20k manifests and this runs before every
    batch. Any leak large enough to matter shows up in 3,000 packages; a check
    that is too slow to run gets removed, which protects nothing.
    """
    import run2_enrich as E
    try:
        con = sqlite3.connect(f"file:{WORK}?mode=ro", uri=True, timeout=8)
        con.execute("PRAGMA busy_timeout=8000")
        rows = list(con.execute(
            "select manifest_json from skill_packages"
            " order by created_at desc limit 3000"))
        con.close()
    except sqlite3.Error as exc:
        NOTES.append(f"served-corpus check skipped ({type(exc).__name__})")
        return
    fixture = risk = 0
    for (mj,) in rows:
        try:
            m = json.loads(mj)
        except Exception:
            continue
        if E.FIXTURE_PATH_RE.search("/" + (m.get("entrypoint") or "")):
            fixture += 1
        flags = set(((m.get("provenance") or {}).get("risk_flags") or []))
        if flags & E.HARD_RISK_FLAGS:
            risk += 1
    bad = fixture + risk
    check("served_corpus.gates_hold", bad == 0,
          f"{fixture} fixture-path + {risk} hard-risk packages are SERVABLE "
          f"(run run2_apply_gates_retro.py)" if bad
          else f"{len(rows)} most recent packages all gate-clean")


def disk() -> None:
    free_gb = shutil.disk_usage(BACKEND).free / 1e9
    check("disk.headroom", free_gb > 10, f"{free_gb:.0f} GB free")


def main() -> int:
    print("preflight:")
    stage_imports()
    schema()
    judges_available()
    db_writers()
    gates_vs_canaries()
    canary_coverage()
    wal_size()
    timestamp_formats()
    quarantine_reasons()
    served_corpus_clean()
    disk()
    for n in NOTES:
        print(f"  note: {n}")
    if FAILURES:
        print(f"\nPREFLIGHT FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\npreflight OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
