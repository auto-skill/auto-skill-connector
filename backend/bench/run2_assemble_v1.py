#!/usr/bin/env python3
"""Corpus v1 / step 5 — assemble the v1 snapshot manifest and measure it.

Collects every judged skill across all run-2 batches, deduplicates by normalized
entrypoint content hash, elects a canonical per cluster, and emits a manifest with
package hashes, enrichment prompt/model versions and the frozen gate config.

Canonical election (plan order):
  1. root repo over fork copy        — repo name == skill dir name, or fewest path segments
  2. upstream over aggregator mirror — de-prioritise known aggregator/marketplace repos
  3. deterministic tiebreak on (repo, path) so reruns are stable

Read-only over the batch artifacts. Writes only CORPUS_V1_MANIFEST.json + metrics.
"""
from __future__ import annotations

import glob
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
DB = BACKEND / "enrichment_v1.db"
LIB = BACKEND / "skills_library_v1"
OUT = BENCH / "CORPUS_V1_MANIFEST.json"
METRICS = BENCH / "CORPUS_V1_METRICS.json"
CANARIES = BACKEND / "evals" / "corpus_canaries.json"

AGGREGATOR_RE = re.compile(
    r"(marketplace|awesome|aggregat|mirror|collection|registry|skillstore|skills-?hub|"
    r"catalog|directory|showcase|curated)", re.I)


def batch_files() -> list[Path]:
    return sorted(BENCH.glob("run2_combined_*.json"))


def fetch_caches() -> dict:
    out = {}
    for f in sorted(BENCH.glob("run2_fetch_*.json")):
        try:
            out.update(json.loads(f.read_text()))
        except Exception:
            pass
    return out


def canonical_rank(repo: str, path: str, skill_dir: str) -> tuple:
    """Lower tuple sorts first = more canonical."""
    owner, _, name = repo.partition("/")
    dirname = Path(skill_dir).name if skill_dir else ""
    is_aggregator = 1 if AGGREGATOR_RE.search(repo) else 0
    # repo whose name matches the skill it ships is very likely the upstream home
    name_match = 0 if dirname and name.lower().startswith(dirname.lower()[:12]) else 1
    depth = len(path.split("/"))
    return (is_aggregator, name_match, depth, len(path), repo.lower(), path.lower())


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    caches = fetch_caches()

    # ---- gather every judged row across all batches, newest prompt version wins ----
    rows: dict[str, dict] = {}          # norm_hash -> best row
    per_hash_locations: dict[str, list] = defaultdict(list)
    seen_batches = []
    for bf in batch_files():
        try:
            d = json.loads(bf.read_text())
        except Exception:
            continue
        seen_batches.append(bf.name)
        for r in d.get("rows", []):
            nh = r.get("norm_hash")
            if not nh or nh.startswith("nofetch:"):
                continue
            f = caches.get(r["skill_id"], {})
            loc = {"skill_id": r["skill_id"], "repo": r.get("repo"),
                   "entry_path": r.get("entry_path"),
                   "skill_dir": f.get("skill_dir"), "url": r.get("url"),
                   "source": r.get("source"),
                   "symlink": bool(r.get("entrypoint_symlink"))}
            if loc["repo"]:
                per_hash_locations[nh].append(loc)
            prev = rows.get(nh)
            if prev is None or (r.get("label") == "included" and prev.get("label") != "included"):
                rows[nh] = r

    # ---- elect canonical per content-hash cluster ----
    manifest = []
    label_counts = Counter()
    dup_cluster_sizes = Counter()
    closure_stats = Counter()
    link_rot_by_source = Counter()
    spec_bins = Counter()

    for nh, r in sorted(rows.items()):
        locs = per_hash_locations.get(nh) or []
        if locs:
            locs_sorted = sorted(
                locs, key=lambda l: canonical_rank(l["repo"] or "", l["entry_path"] or "",
                                                   l["skill_dir"] or ""))
            canonical = locs_sorted[0]
            alternates = locs_sorted[1:]
        else:
            canonical, alternates = {}, []
        label = r.get("label")
        label_counts[label] += 1
        dup_cluster_sizes[len(locs) or 1] += 1
        p = r.get("primary") or {}
        cl = r.get("closure") or {}
        closure_stats["with_closure"] += 1 if cl else 0
        closure_stats["files_present"] += len(cl.get("already_present", []))
        closure_stats["files_fetched"] += len(cl.get("fetched", []))
        closure_stats["files_missing"] += len(cl.get("missing", []))
        closure_stats["unanchorable"] += len(r.get("closure_unanchorable") or [])
        if r.get("fetch_status") == "source_deleted":
            link_rot_by_source[r.get("source") or "unknown"] += 1
        sp = p.get("specificity")
        if sp is not None:
            spec_bins["0.0-0.2" if sp <= .2 else "0.2-0.4" if sp <= .4 else
                      "0.4-0.6" if sp <= .6 else "0.6-0.8" if sp <= .8 else "0.8-1.0"] += 1

        if label not in ("included", "quarantine"):
            continue
        manifest.append({
            "norm_hash": nh,
            "label": label,
            "quarantine_reason": r.get("reason") if label == "quarantine" else None,
            "name": r.get("name"),
            "canonical": canonical,
            "duplicate_locations": len(alternates),
            "alternates": alternates[:5],
            "entrypoint_object_sha256": (caches.get(r["skill_id"], {}) or {}).get("entry_hash"),
            "entrypoint_symlink": r.get("entrypoint_symlink"),
            "closure": {"fetched": cl.get("fetched", []), "present": cl.get("already_present", []),
                        "missing": cl.get("missing", []),
                        "unanchorable": r.get("closure_unanchorable") or []},
            "enrichment": {
                "prompt_version": "v2.1",
                "primary_model": "gpt-5.6-luna@medium/codex-cli-0.144.6",
                "secondary_model": "claude-haiku-4-5-20251001",
                "is_real_skill": p.get("is_real_skill"),
                "specificity": p.get("specificity"),
                "vendor_convention": p.get("vendor_convention"),
                "summary": p.get("summary"),
                "triggers": p.get("triggers"),
                "risk_flags": p.get("risk_flags"),
                "truncated_input": p.get("truncated_input"),
            },
        })

    # ---- canary recall ----
    canary_defs = json.loads(CANARIES.read_text())["canaries"]
    by_repo_path = {}
    for m in manifest:
        c = m["canonical"]
        if c.get("repo") and c.get("entry_path"):
            by_repo_path[(c["repo"].casefold(), c["entry_path"].casefold())] = m
        for a in m["alternates"]:
            if a.get("repo") and a.get("entry_path"):
                by_repo_path.setdefault((a["repo"].casefold(), a["entry_path"].casefold()), m)
    canary_rows = []
    for c in canary_defs:
        key = (c["parent_repo"].casefold(), c["path"].casefold())
        m = by_repo_path.get(key)
        canary_rows.append({"canary_id": c["id"], "repo": c["parent_repo"], "path": c["path"],
                            "in_v1": bool(m), "label": m["label"] if m else None,
                            "specificity": (m["enrichment"]["specificity"] if m else None)})
    recall = sum(1 for c in canary_rows if c["in_v1"] and c["label"] == "included")

    sightings = con.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
    con.close()

    gate_config = {
        "prompt_version": "v2.1",
        "primary_judge": "gpt-5.6-luna @ medium, codex-cli 0.144.6, sealed (env -i, read-only sandbox, ephemeral)",
        "secondary_judge": "claude-haiku-4-5-20251001, runs on every primary reject",
        "confidence_in_logic": False,
        "specificity": "advisory only, gates nothing",
        "deterministic_prefilters": ["entrypoint_absent", "fetch_failed(retryable)",
                                     "source_deleted(404)", "entrypoint_unreadable",
                                     "symlink_escapes_repo", "symlink_target_missing",
                                     "frontmatter_missing", "frontmatter_invalid",
                                     "body_too_short(<200)", "duplicate_hash"],
        "entry_char_cap": 24000, "truncation": "head 2/3 + tail 1/3, elision marked inline",
        "closure_caps": {"max_files": 20, "max_bytes_per_file": 262144},
        "closure_reanchoring": "longest unique path suffix against this row's tree",
    }

    out = {
        "schema_version": 1,
        "corpus": "v1",
        "run_id": "corpusv1-20260802",
        "source_batches": seen_batches,
        "gate_config": gate_config,
        "counts": {
            "unique_content_hashes_judged": len(rows),
            "in_manifest": len(manifest),
            "labels_all_judged": dict(label_counts),
            "sightings_enumerated": sightings,
        },
        "canary_recall": {"total": len(canary_rows), "included": recall,
                          "pct": round(100.0 * recall / max(1, len(canary_rows)), 1),
                          "rows": canary_rows},
        "skills": manifest,
    }
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")

    metrics = {
        "dup_rate": {
            "clusters": len(rows),
            "clusters_with_duplicates": sum(n for k, n in dup_cluster_sizes.items() if k > 1),
            "cluster_size_histogram": dict(sorted(dup_cluster_sizes.items())),
        },
        "junk_rate": {
            "labels": dict(label_counts),
            "junk_pct": round(100.0 * label_counts.get("excluded_junk", 0) /
                              max(1, sum(label_counts.values())), 1),
        },
        "closure_completeness": dict(closure_stats),
        "link_rot_by_source": dict(link_rot_by_source),
        "specificity_histogram": dict(sorted(spec_bins.items())),
        "canary_recall_pct": out["canary_recall"]["pct"],
    }
    METRICS.write_text(json.dumps(metrics, indent=1), encoding="utf-8")

    print(f"manifest: {len(manifest)} skills from {len(rows)} unique content hashes")
    print("labels:", dict(label_counts))
    print(f"canary recall: {recall}/{len(canary_rows)} ({out['canary_recall']['pct']}%)")
    for c in canary_rows:
        if not (c["in_v1"] and c["label"] == "included"):
            print(f"   MISSING/NOT-INCLUDED: {c['canary_id']} ({c['repo']}) label={c['label']}")
    print("metrics ->", METRICS)
    print("manifest ->", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
