#!/usr/bin/env python3
"""Ingestion analytics: periodic funnel snapshots + historical backfill + export.

The run needs a time-series record, not just point-in-time status files (those
are overwritten and cannot be graphed). Every judgement, sighting, package and
harvested repo already carries a timestamp, so the curve is reconstructable
back to the start of the run — `--backfill` does exactly that once, and the
5-minute timer keeps it growing live.

    metrics_snapshots (in enrichment_v1.db; one row per sample)
      ts, source            'live' | 'backfill'
      sightings_skillmd     discovery: SKILL.md sightings seen so far
      repos_harvested       tree-harvest coverage (repos)
      distinct_skill_shas   unique SKILL.md blob shas known (dedupe backbone)
      skillmd_path_rows     total SKILL.md paths harvested (dup ratio = rows/shas)
      judged_unique         distinct norm_hash with a primary/deterministic verdict
      inherited             sightings whose verdict transferred by blob sha
      included / excluded / pending / quarantine   unique labels across batches
      packages              servable package count
      luna_calls_day, luna_tokens_in_day, luna_tokens_out_day   spend today
      batch_size, quality_streak                   ladder state
      gh_core_remaining     API quota at sample time (correlates stalls)

Usage:
    run2_metrics.py                 one live snapshot (what the timer runs)
    run2_metrics.py --backfill      reconstruct hourly history from row timestamps
    run2_metrics.py --export out.csv   dump the series for graphing
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
DB = BACKEND / "enrichment_v1.db"
WORK = BACKEND / "corpus_v0_work.sqlite"
# Snapshots live in their OWN db: the enrichment db has three concurrent
# writers, and analytics must never contend with ingestion for a lock.
SNAP = BACKEND / "analytics_v1.db"
STATE = Path("/srv/mobile-codex/autoskill-daemon/state")
STATUS = Path("/srv/mobile-codex/autoskill-daemon/status")

COLS = ("ts", "source", "sightings_skillmd", "repos_harvested", "distinct_skill_shas",
        "skillmd_path_rows", "judged_unique", "inherited", "included", "excluded",
        "pending", "quarantine", "packages", "luna_calls_day", "luna_tokens_in_day",
        "luna_tokens_out_day", "batch_size", "quality_streak", "gh_core_remaining", "wal_mb")


def ensure(con: sqlite3.Connection) -> None:
    con.execute(f"""CREATE TABLE IF NOT EXISTS metrics_snapshots (
        {", ".join(c + (" TEXT" if c in ("ts", "source") else " INTEGER") for c in COLS)},
        PRIMARY KEY (ts, source))""")
    # CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so a column
    # added to COLS later would silently never exist and every write would fail.
    have = {r[1] for r in con.execute("PRAGMA table_info(metrics_snapshots)")}
    for c in COLS:
        if c not in have:
            con.execute(f"ALTER TABLE metrics_snapshots ADD COLUMN {c} "
                        f"{'TEXT' if c in ('ts', 'source') else 'INTEGER'}")
    # The final labels only live in combined batch artifacts. Cache the latest
    # label by norm hash so a five-minute sample does not re-parse the entire
    # historical corpus and compete with enrichment for CPU.
    con.execute("""CREATE TABLE IF NOT EXISTS metrics_label_current (
        norm_hash TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        batch_num INTEGER NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS metrics_label_files (
        path TEXT PRIMARY KEY,
        batch_num INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        size INTEGER NOT NULL
    )""")
    con.commit()


def q1(con, sql, default=0):
    try:
        v = con.execute(sql).fetchone()[0]
        return v if v is not None else default
    except Exception:
        return default


_COMBINED_BATCH = re.compile(r"run2_combined_b(\d+)\.json$")


def combined_files() -> list[tuple[int, Path, int, int]]:
    """Return combined artifacts in numeric batch order with change metadata."""
    files = []
    for raw in glob.glob(str(BENCH / "run2_combined_b*.json")):
        path = Path(raw)
        match = _COMBINED_BATCH.search(path.name)
        if not match:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((int(match.group(1)), path, stat.st_mtime_ns, stat.st_size))
    return sorted(files, key=lambda item: item[0])


def load_labels(con: sqlite3.Connection, files: list[tuple[int, Path, int, int]]) -> None:
    """Incrementally materialize final labels from batch artifacts.

    A repaired historical artifact is rare, but must not leave stale analytics.
    If any already-seen artifact changes or disappears, rebuild once; ordinary
    runs parse only newly completed batches.
    """
    existing = {
        path: (batch_num, mtime_ns, size)
        for path, batch_num, mtime_ns, size in con.execute(
            "SELECT path, batch_num, mtime_ns, size FROM metrics_label_files"
        )
    }
    current = {str(path): (batch_num, mtime_ns, size)
               for batch_num, path, mtime_ns, size in files}
    rebuild = bool(existing) and any(
        current.get(path) != meta for path, meta in existing.items()
    )
    if not existing and files:
        rebuild = True
    if rebuild:
        con.execute("DELETE FROM metrics_label_current")
        con.execute("DELETE FROM metrics_label_files")
        existing.clear()

    for batch_num, path, mtime_ns, size in files:
        if str(path) in existing:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = [(r["norm_hash"], r.get("label", "?"), batch_num)
                for r in payload.get("rows", []) if r.get("norm_hash")]
        con.executemany(
            """INSERT INTO metrics_label_current(norm_hash, label, batch_num)
               VALUES (?, ?, ?)
               ON CONFLICT(norm_hash) DO UPDATE SET
                 label=excluded.label, batch_num=excluded.batch_num
               WHERE excluded.batch_num >= metrics_label_current.batch_num""",
            rows,
        )
        con.execute(
            "INSERT OR REPLACE INTO metrics_label_files(path, batch_num, mtime_ns, size)"
            " VALUES (?, ?, ?, ?)",
            (str(path), batch_num, mtime_ns, size),
        )


def label_counts(con: sqlite3.Connection) -> dict:
    """Unique-by-norm-hash label counts without recurring historical scans."""
    load_labels(con, combined_files())
    out = {"included": 0, "excluded_junk": 0, "pending": 0, "quarantine": 0}
    for label, count in con.execute(
        "SELECT label, count(*) FROM metrics_label_current GROUP BY label"
    ):
        if label in out:
            out[label] = count
    return out


def gh_quota() -> int:
    try:
        sys.path.insert(0, str(BENCH))
        from run2_enrich import github_token
        req = urllib.request.Request("https://api.github.com/rate_limit", headers={
            "Authorization": f"Bearer {github_token()}", "User-Agent": "autoskill-metrics"})
        with urllib.request.urlopen(req, timeout=15) as fh:
            return json.load(fh)["resources"]["core"]["remaining"]
    except Exception:
        return -1


def status_line(name: str) -> str:
    try:
        return (STATUS / f"{name}.status").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def status_int(line: str, name: str) -> int | None:
    match = re.search(rf"\b{re.escape(name)}=(\d[\d,]*)", line)
    return int(match.group(1).replace(",", "")) if match else None


def status_fraction_numerator(line: str, name: str) -> int | None:
    match = re.search(rf"\b{re.escape(name)}=(\d[\d,]*)/", line)
    return int(match.group(1).replace(",", "")) if match else None


def snapshot() -> dict:
    """Write a cheap live sample without scanning the serving databases.

    The ingestion daemons already publish atomic one-line progress state. Using
    it avoids multi-gigabyte SQLite scans and keeps this observational timer
    from contending with the workers it is meant to monitor. Fields without a
    cheap authoritative source are left NULL rather than estimated.
    """
    tree = status_line("treeharvest")
    enrich = status_line("enrich")
    wal = Path(str(DB) + "-wal")
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "live",
        "sightings_skillmd": status_int(status_line("sweep"), "sightings"),
        "repos_harvested": status_fraction_numerator(tree, "repos_harvested"),
        "distinct_skill_shas": status_int(tree, "distinct_skill_shas"),
        "skillmd_path_rows": None,
        "judged_unique": status_int(enrich, "judged_hashes"),
        "inherited": None,
        "included": None,
        "excluded": None,
        "pending": None,
        "quarantine": None,
        "packages": status_int(enrich, "packages"),
        "luna_calls_day": status_fraction_numerator(enrich, "luna_today"),
        "luna_tokens_in_day": None,
        "luna_tokens_out_day": None,
        "batch_size": int((STATE / "batch_size").read_text().strip() or 0)
                      if (STATE / "batch_size").exists() else None,
        "quality_streak": int((STATE / "quality_streak").read_text().strip() or 0)
                          if (STATE / "quality_streak").exists() else None,
        "gh_core_remaining": None,
        "wal_mb": round(wal.stat().st_size / 1e6, 1) if wal.exists() else 0,
    }


def maintain_wal() -> float:
    """Checkpoint what is available without waiting on ingestion workers."""
    try:
        con = sqlite3.connect(DB, timeout=0)
        con.execute("PRAGMA busy_timeout=0")
        con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        # If readers are active this returns busy immediately. If not, it also
        # returns the preallocated file space to the filesystem.
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        con.close()
    except sqlite3.Error:
        pass
    wal = Path(str(DB) + "-wal")
    return round(wal.stat().st_size / 1e6, 1) if wal.exists() else 0


def write(con: sqlite3.Connection, row: dict) -> None:
    con.execute(f"insert or replace into metrics_snapshots ({','.join(COLS)})"
                f" values ({','.join('?' * len(COLS))})",
                [row.get(c) for c in COLS])
    con.commit()


def backfill(con: sqlite3.Connection) -> int:
    """Hourly cumulative history reconstructed from row timestamps.

    Counters that cannot be reconstructed (labels, ladder state, quota) are
    left NULL so a graph shows a gap rather than a fabricated zero.
    """
    e = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    w = sqlite3.connect(f"file:{WORK}?mode=ro", uri=True)
    t0 = min(x for x in [
        q1(e, "select min(observed_at) from sightings", None),
        q1(e, "select min(created_at) from enrichments", None)] if x)
    start = datetime.fromisoformat(t0.replace("Z", "+00:00")).replace(
        minute=0, second=0, microsecond=0)
    hours = []
    t = start
    while t <= datetime.now(timezone.utc):
        hours.append(t)
        t = t.__class__.fromtimestamp(t.timestamp() + 3600, tz=timezone.utc)
    n = 0
    for h in hours:
        cut = h.isoformat()
        row = {c: None for c in COLS}
        row.update({
            "ts": cut, "source": "backfill",
            "sightings_skillmd": q1(e, "select count(*) from sightings"
                f" where path like '%SKILL.md' and observed_at <= '{cut}'"),
            "repos_harvested": q1(e, "select count(*) from repo_tree_meta"
                                     f" where harvested_at <= '{cut}'"),
            "distinct_skill_shas": q1(e, "select count(distinct sha) from repo_trees"
                f" where path like '%SKILL.md' and harvested_at <= '{cut}'"),
            "skillmd_path_rows": q1(e, "select count(*) from repo_trees"
                f" where path like '%SKILL.md' and harvested_at <= '{cut}'"),
            "judged_unique": q1(e, "select count(distinct norm_hash) from enrichments"
                " where judge_role in ('primary','deterministic')"
                f" and created_at <= '{cut}'"),
            "inherited": q1(e, "select count(*) from enrichments"
                f" where model_snapshot='inherit/blob-sha' and created_at <= '{cut}'"),
            "packages": q1(w, f"select count(*) from skill_packages where created_at <= '{cut}'"),
        })
        write(con, row)
        n += 1
    e.close(); w.close()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--export", type=Path)
    args = ap.parse_args()

    con = sqlite3.connect(SNAP, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    ensure(con)

    if args.backfill:
        n = backfill(con)
        print(f"backfilled {n} hourly snapshots")
    elif args.export:
        import csv
        rows = con.execute(f"select {','.join(COLS)} from metrics_snapshots"
                           " order by ts").fetchall()
        with open(args.export, "w", newline="") as fh:
            wcsv = csv.writer(fh)
            wcsv.writerow(COLS)
            wcsv.writerows(rows)
        print(f"exported {len(rows)} snapshots -> {args.export}")
    else:
        row = snapshot()
        row["wal_mb"] = maintain_wal()
        write(con, row)
        print(json.dumps({k: row[k] for k in
                          ("ts", "sightings_skillmd", "distinct_skill_shas",
                           "judged_unique", "inherited", "included", "packages",
                           "gh_core_remaining")}))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
