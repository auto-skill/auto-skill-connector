#!/usr/bin/env python3
"""Compile the judged corpus + verdicts into a versioned knowledge-graph snapshot.

Build sequence (each stage checkpoints into build/ and is independently
resumable; `--stage all` runs whatever is not yet done):

  A registry    canonical skill revisions: canonical_id -> norm_hash + metadata
  B judgments   aggregate verdict evidence BY norm_hash (never one node per
                judgment): ok/real counts, independent-judge count, specificity,
                triggers, risk flags. status='ok' is the ONLY source of positive
                evidence; failed/timeout rows are counted separately as
                retry_evidence and never strengthen an edge.
  C extract     deterministic content extraction per skill: tools (code-fence
                and inline commands), artifacts (file types), environments
                (languages + vendor convention), boundary cues (negative
                triggers, credential requirements), prerequisite cues.
  D assemble    nodes + evidence-weighted edges + suppression boundaries.
  E snapshot    versioned read-only graph.sqlite + manifest (source hashes,
                compiler git rev, counts, rollbackable snapshot id).

Node types: skill_revision, capability, tool, artifact, environment, task_intent
Edge types: provides, uses_tool, consumes, produces, runs_in, requires,
            substitutes, conflicts_with, member_of
Boundaries:  negative_trigger, requires_credential, risk_flag  (suppression at
             query time -- a boundary never deletes evidence, it gates it)

Every edge carries evidence_json naming its sources, so any edge can be
audited back to raw SKILL.md structure or aggregated verdicts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTORESEARCH = HERE.parent
BENCH = AUTORESEARCH.parent
BACKEND = BENCH.parent
sys.path.insert(0, str(BENCH))
from run2_enrich import norm_hash_of  # noqa: E402

SKILLS_DB = BACKEND / "skills_judged_v2.db"
LIB_FILES = BACKEND / "judged_library_v2" / "files"
ENRICH_DB = BACKEND / "enrichment_v1.db"
BUILD = HERE / "build"
SNAPSHOTS = HERE / "snapshots"

# ---- thresholds (recorded in the manifest; changing them is a new compiler
# version, not a silent drift) ----
CAP_MIN_SKILLS = 2          # capability node needs >=2 skills providing it
TOOL_MIN_SKILLS = 5         # tool node needs >=5 skills using it
ARTIFACT_MIN_SKILLS = 10
SUBST_MIN_SHARED = 3        # substitutes: >=3 shared capabilities
SUBST_MAX_DF = 200          # capabilities more common than this don't pair
INTENT_MIN_COOC = 5         # task-intent union threshold
INTENT_MIN_PMI = 3.0

CMD_RE = re.compile(r"^\s*\$?\s*([a-z][a-z0-9_.+-]{1,24})(?=\s|$)")
FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`\n]{2,60})`")
EXT_RE = re.compile(r"\.(csv|json|jsonl|yaml|yml|toml|xml|pdf|docx|xlsx|md|txt|png|jpg|svg|html|sql|parquet|proto|ipynb|zip|tar|gz|env|log|sqlite|db)\b", re.I)
NEG_RE = re.compile(r"(do not use|don't use|not (?:for|suitable|intended)|avoid (?:using|when)|never use|only works? (?:with|on|when))([^\n.]{0,120})", re.I)
CRED_RE = re.compile(r"(requires?|needs?|set|export)\s+(?:an?\s+)?([A-Z][A-Z0-9_]{2,40}_(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)|api[ -]?key|access token|auth(?:entication)? token|service account|credentials)", re.I)
CONFLICT_RE = re.compile(r"(incompatible with|conflicts? with|cannot be used with|breaks when used with)([^\n.]{0,100})", re.I)
PREREQ_RE = re.compile(r"(?:before (?:running|using)|requires?|must (?:first )?(?:install|have))\s+`?([a-zA-Z][a-zA-Z0-9_.+-]{1,30})`?", re.I)
NOT_TOOLS = {"the", "a", "an", "this", "your", "you", "it", "if", "then", "else", "for",
             "and", "or", "not", "is", "in", "on", "to", "of", "with", "cd", "ls",
             "echo", "cat", "true", "false", "http", "https", "www", "e.g", "i.e"}
SHELL_LANGS = {"", "bash", "sh", "shell", "zsh", "console", "terminal", "cmd", "powershell"}


def norm_trigger(t: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()[:80]


def open_db(path: Path, ro: bool = True) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro" if ro else str(path),
                          uri=ro, timeout=120)
    con.execute("pragma busy_timeout=120000")
    if not ro:
        con.executescript("pragma journal_mode=WAL; pragma synchronous=NORMAL;")
    return con


def stage_done(name: str) -> bool:
    return (BUILD / f".done_{name}").exists()


def mark_done(name: str, info: dict):
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / f".done_{name}").write_text(json.dumps({**info, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))


# ---------------------------------------------------------------- stage A
def stage_registry():
    """canonical_id -> norm_hash + metadata. norm_hash is recomputed from the
    exact library bytes so the judgment join can never bind to drifted text."""
    out = open_db(BUILD / "registry.sqlite", ro=False)
    out.executescript("""
      create table if not exists registry (
        canonical_id text primary key, norm_hash text, name text, url text,
        summary text, triggers_json text, category text,
        quality real, prominence real, risk_flags_json text);
      create index if not exists reg_nh on registry(norm_hash);
    """)
    have = {r[0] for r in out.execute("select canonical_id from registry")}
    src = open_db(SKILLS_DB)
    rows = src.execute(
        "select canonical_id, name, url, capability_summary, triggers,"
        "       category, quality_score, prominence_score, risk_flags from skills").fetchall()
    src.close()
    n = skipped = 0
    batch = []
    for cid, name, url, summary, triggers, cat, q, p, rf in rows:
        if cid in have:
            continue
        f = LIB_FILES / f"{cid}.md"
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue
        batch.append((cid, norm_hash_of(text), name, url, summary, triggers,
                      cat, (q or 0) / 100.0, p or 0.0, rf))
        if len(batch) >= 2000:
            out.executemany("insert or replace into registry values (?,?,?,?,?,?,?,?,?,?)", batch)
            out.commit()
            n += len(batch)
            batch = []
            if n % 50000 < 2000:
                print(f"  registry {n:,}", flush=True)
    if batch:
        out.executemany("insert or replace into registry values (?,?,?,?,?,?,?,?,?,?)", batch)
        out.commit()
        n += len(batch)
    total = out.execute("select count(*) from registry").fetchone()[0]
    out.close()
    print(f"  registry: {total:,} revisions (+{n:,} new, {skipped} unreadable)")
    mark_done("registry", {"rows": total, "skipped": skipped})


# ---------------------------------------------------------------- stage B
def stage_judgments():
    """Aggregate verdicts by norm_hash. THE quality rule lives here:
    only status='ok' rows produce positive evidence; failed rows are counted
    as retry_evidence and contribute nothing to any weight."""
    reg = open_db(BUILD / "registry.sqlite")
    wanted = {r[0] for r in reg.execute("select distinct norm_hash from registry")}
    reg.close()
    print(f"  aggregating verdicts for {len(wanted):,} norm_hashes", flush=True)

    out = open_db(BUILD / "judgments_agg.sqlite", ro=False)
    out.executescript("""
      create table if not exists agg (
        norm_hash text primary key, n_ok integer, n_real integer,
        n_judges integer, mean_specificity real, triggers_json text,
        risk_flags_json text, n_retry_evidence integer);
    """)
    have = {r[0] for r in out.execute("select norm_hash from agg")}
    src = open_db(ENRICH_DB)
    cur = src.execute(
        "select norm_hash, model_snapshot, status, output_json"
        "  from enrichments where judge_role='primary'")
    acc: dict[str, dict] = {}
    n_scan = 0
    for nh, snap, status, oj in cur:
        n_scan += 1
        if n_scan % 500000 == 0:
            print(f"    scanned {n_scan:,} verdict rows", flush=True)
        if nh not in wanted or nh in have:
            continue
        a = acc.setdefault(nh, {"ok": 0, "real": 0, "judges": set(), "spec": [],
                                "trig": Counter(), "risk": set(), "retry": 0})
        if status != "ok":
            a["retry"] += 1          # retry evidence ONLY -- never positive
            continue
        a["ok"] += 1
        a["judges"].add(snap or "?")
        try:
            d = json.loads(oj)
        except Exception:
            continue
        if d.get("is_real_skill") is True:
            a["real"] += 1
            v = d.get("specificity")
            if isinstance(v, (int, float)):
                a["spec"].append(float(v))
            for t in (d.get("triggers") or [])[:8]:
                if isinstance(t, str) and t.strip():
                    a["trig"][norm_trigger(t)] += 1
            for rf in (d.get("risk_flags") or []):
                if isinstance(rf, str):
                    a["risk"].add(rf[:60])
    src.close()
    rows = [(nh, a["ok"], a["real"], len(a["judges"]),
             (sum(a["spec"]) / len(a["spec"])) if a["spec"] else 0.0,
             json.dumps([t for t, _ in a["trig"].most_common(10)]),
             json.dumps(sorted(a["risk"])), a["retry"])
            for nh, a in acc.items()]
    out.executemany("insert or replace into agg values (?,?,?,?,?,?,?,?)", rows)
    out.commit()
    total = out.execute("select count(*) from agg").fetchone()[0]
    two_plus = out.execute("select count(*) from agg where n_judges>=2").fetchone()[0]
    out.close()
    print(f"  judgments: {total:,} aggregated ({two_plus:,} independently double-judged)")
    mark_done("judgments", {"rows": total, "double_judged": two_plus, "scanned": n_scan})


# ---------------------------------------------------------------- stage C
def stage_extract():
    out = open_db(BUILD / "extractions.sqlite", ro=False)
    out.executescript("""
      create table if not exists ext (
        canonical_id text primary key, tools_json text, langs_json text,
        artifacts_json text, neg_json text, cred_json text,
        conflict_json text, prereq_json text);
    """)
    have = {r[0] for r in out.execute("select canonical_id from ext")}
    reg = open_db(BUILD / "registry.sqlite")
    ids = [r[0] for r in reg.execute("select canonical_id from registry")]
    reg.close()
    n = 0
    batch = []
    for cid in ids:
        if cid in have:
            continue
        try:
            text = (LIB_FILES / f"{cid}.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tools, langs = Counter(), Counter()
        for lang, body in FENCE_RE.findall(text):
            lang = lang.lower()
            if lang and lang not in SHELL_LANGS:
                langs[lang] += 1
            if lang in SHELL_LANGS:
                for line in body.splitlines()[:60]:
                    m = CMD_RE.match(line)
                    if m and m.group(1) not in NOT_TOOLS:
                        tools[m.group(1)] += 1
        for frag in INLINE_CODE_RE.findall(text)[:200]:
            m = CMD_RE.match(frag)
            if m and m.group(1) not in NOT_TOOLS and (" " in frag or "-" in frag):
                tools[m.group(1)] += 1
        arts = Counter(e.lower() for e in EXT_RE.findall(text))
        neg = [(" ".join(g)).strip()[:150] for g in NEG_RE.findall(text)[:8]]
        cred = sorted({m[1][:50] for m in CRED_RE.findall(text)})[:8]
        confl = [(" ".join(g)).strip()[:150] for g in CONFLICT_RE.findall(text)[:5]]
        prereq = sorted({p.lower() for p in PREREQ_RE.findall(text)
                         if p.lower() not in NOT_TOOLS and len(p) > 2})[:10]
        batch.append((cid, json.dumps(tools.most_common(15)), json.dumps(langs.most_common(6)),
                      json.dumps(arts.most_common(10)), json.dumps(neg), json.dumps(cred),
                      json.dumps(confl), json.dumps(prereq)))
        if len(batch) >= 2000:
            out.executemany("insert or replace into ext values (?,?,?,?,?,?,?,?)", batch)
            out.commit()
            n += len(batch)
            batch = []
            if n % 50000 < 2000:
                print(f"  extract {n:,}", flush=True)
    if batch:
        out.executemany("insert or replace into ext values (?,?,?,?,?,?,?,?)", batch)
        out.commit()
        n += len(batch)
    total = out.execute("select count(*) from ext").fetchone()[0]
    out.close()
    print(f"  extract: {total:,} skills processed")
    mark_done("extract", {"rows": total})


# ---------------------------------------------------------------- stage D
def edge_weight(n_ok: int, n_judges: int, spec: float) -> float:
    import math
    if n_ok <= 0:
        return 0.0
    w = (1.0 + math.log2(n_ok)) * max(spec, 0.05)
    if n_judges >= 2:
        w *= 1.25   # independent double-judgement is stronger evidence
    return round(w, 4)


def stage_assemble():
    reg = open_db(BUILD / "registry.sqlite")
    agg = open_db(BUILD / "judgments_agg.sqlite")
    ext = open_db(BUILD / "extractions.sqlite")
    gdb_path = BUILD / "graph_staging.sqlite"
    if gdb_path.exists():
        gdb_path.unlink()
    g = open_db(gdb_path, ro=False)
    g.executescript("""
      create table nodes (node_id text primary key, node_type text, label text, meta_json text);
      create table edges (src text, dst text, edge_type text, weight real, evidence_json text);
      create table boundaries (canonical_id text, boundary_type text, evidence text);
      create index e_src on edges(src); create index e_dst on edges(dst);
      create index e_type on edges(edge_type); create index n_type on nodes(node_type);
    """)
    aggm = {r[0]: r for r in agg.execute("select * from agg")}
    agg.close()
    extm = {r[0]: r for r in ext.execute("select * from ext")}
    ext.close()

    cap_count, tool_count, art_count, env_count = Counter(), Counter(), Counter(), Counter()
    skills = []
    for (cid, nh, name, url, summary, trig_j, cat, q, p, rf_j) in reg.execute("select * from registry"):
        a = aggm.get(nh)
        caps = set()
        if a:
            caps.update(json.loads(a[5]))
        for t in json.loads(trig_j or "[]"):
            if isinstance(t, str):
                caps.add(norm_trigger(t))
        caps.discard("")
        e = extm.get(cid)
        tools = [t for t, c in json.loads(e[1])] if e else []
        langs = [l for l, c in json.loads(e[2])] if e else []
        arts = [x for x, c in json.loads(e[3])] if e else []
        for c in caps:
            cap_count[c] += 1
        for t in tools:
            tool_count[t] += 1
        for x in arts:
            art_count[x] += 1
        for l in langs:
            env_count[l] += 1
        if cat:
            env_count[cat] += 1
        skills.append((cid, nh, name, url, summary, cat, q, p, rf_j, caps, tools, langs, arts, e, a))
    reg.close()

    caps_kept = {c for c, n in cap_count.items() if n >= CAP_MIN_SKILLS}
    tools_kept = {t for t, n in tool_count.items() if n >= TOOL_MIN_SKILLS}
    arts_kept = {x for x, n in art_count.items() if n >= ARTIFACT_MIN_SKILLS}
    envs_kept = {l for l, n in env_count.items() if n >= TOOL_MIN_SKILLS}
    print(f"  nodes: {len(skills):,} skills, {len(caps_kept):,} capabilities,"
          f" {len(tools_kept):,} tools, {len(arts_kept):,} artifacts, {len(envs_kept):,} envs", flush=True)

    nodes, edges, bounds = [], [], []
    for cid, nh, name, url, summary, cat, q, p, rf_j, caps, tools, langs, arts, e, a in skills:
        n_ok, n_judges, spec = (a[1], a[3], a[4]) if a else (0, 0, 0.0)
        retry = a[7] if a else 0
        nodes.append((f"s:{cid}", "skill_revision", name or cid[:12],
                      json.dumps({"norm_hash": nh, "url": url, "quality": q,
                                  "prominence": p, "n_ok": n_ok, "n_judges": n_judges,
                                  "retry_evidence": retry})))
        w = edge_weight(n_ok, n_judges, spec if spec else q)
        ev = json.dumps({"n_ok": n_ok, "n_judges": n_judges, "spec": spec, "src": "judgments"})
        for c in caps:
            if c in caps_kept:
                edges.append((f"s:{cid}", f"c:{c}", "provides", w, ev))
        for t in tools:
            if t in tools_kept:
                edges.append((f"s:{cid}", f"t:{t}", "uses_tool", 1.0,
                              json.dumps({"src": "content_extraction"})))
        for x in arts:
            if x in arts_kept:
                edges.append((f"s:{cid}", f"a:{x}", "consumes", 0.5,
                              json.dumps({"src": "extension_mention", "direction": "undetermined"})))
        for l in langs + ([cat] if cat else []):
            if l in envs_kept:
                edges.append((f"s:{cid}", f"e:{l}", "runs_in", 1.0,
                              json.dumps({"src": "fence_lang_or_vendor"})))
        if e:
            for neg in json.loads(e[4]):
                bounds.append((cid, "negative_trigger", neg))
            for cred in json.loads(e[5]):
                bounds.append((cid, "requires_credential", cred))
            for pre in json.loads(e[7]):
                if pre in tools_kept:
                    edges.append((f"s:{cid}", f"t:{pre}", "requires", 1.0,
                                  json.dumps({"src": "prereq_cue"})))
            for confl in json.loads(e[6]):
                bounds.append((cid, "conflict_cue", confl))
        for rf in json.loads(rf_j or "[]"):
            bounds.append((cid, "risk_flag", str(rf)[:120]))

    for c in caps_kept:
        nodes.append((f"c:{c}", "capability", c, json.dumps({"df": cap_count[c]})))
    for t in tools_kept:
        nodes.append((f"t:{t}", "tool", t, json.dumps({"df": tool_count[t]})))
    for x in arts_kept:
        nodes.append((f"a:{x}", "artifact", x, json.dumps({"df": art_count[x]})))
    for l in envs_kept:
        nodes.append((f"e:{l}", "environment", l, json.dumps({"df": env_count[l]})))

    # substitutes: shared-capability Jaccard through a bounded inverted index
    inv = defaultdict(list)
    for i, s in enumerate(skills):
        for c in s[9]:
            if c in caps_kept and cap_count[c] <= SUBST_MAX_DF:
                inv[c].append(i)
    pair_shared = Counter()
    for c, members in inv.items():
        if len(members) < 2 or len(members) > 50:
            continue
        for ii in range(len(members)):
            for jj in range(ii + 1, len(members)):
                pair_shared[(members[ii], members[jj])] += 1
    n_subst = 0
    for (i, j), shared in pair_shared.items():
        if shared < SUBST_MIN_SHARED:
            continue
        si, sj = skills[i], skills[j]
        union = len(si[9] | sj[9])
        jac = shared / union if union else 0
        edges.append((f"s:{si[0]}", f"s:{sj[0]}", "substitutes", round(jac, 3),
                      json.dumps({"shared_caps": shared, "jaccard": round(jac, 3)})))
        n_subst += 1

    # task-intent clusters: union-find over high-PMI capability co-occurrence
    cooc = Counter()
    for s in skills:
        cs = sorted(c for c in s[9] if c in caps_kept and cap_count[c] <= SUBST_MAX_DF)
        for ii in range(len(cs)):
            for jj in range(ii + 1, min(ii + 6, len(cs))):
                cooc[(cs[ii], cs[jj])] += 1
    import math
    N = len(skills)
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (c1, c2), n12 in cooc.items():
        if n12 < INTENT_MIN_COOC:
            continue
        pmi = math.log((n12 * N) / (cap_count[c1] * cap_count[c2]))
        if pmi >= INTENT_MIN_PMI:
            parent[find(c1)] = find(c2)
    clusters = defaultdict(list)
    for c in list(parent):
        clusters[find(c)].append(c)
    n_int = 0
    for root, members in clusters.items():
        if len(members) < 2:
            continue
        label = min(members, key=len)
        iid = f"i:{hashlib.sha256(label.encode()).hexdigest()[:10]}"
        nodes.append((iid, "task_intent", label, json.dumps({"n_caps": len(members)})))
        for c in members:
            edges.append((f"c:{c}", iid, "member_of", 1.0, json.dumps({"src": "pmi_cluster"})))
        n_int += 1

    g.executemany("insert or replace into nodes values (?,?,?,?)", nodes)
    g.executemany("insert into edges values (?,?,?,?,?)", edges)
    g.executemany("insert into boundaries values (?,?,?)", bounds)
    g.commit()
    stats = {"nodes": len(nodes), "edges": len(edges), "boundaries": len(bounds),
             "substitutes": n_subst, "intents": n_int}
    g.close()
    print(f"  assemble: {stats}")
    mark_done("assemble", stats)


# ---------------------------------------------------------------- stage E
def stage_snapshot():
    import shutil
    staging = BUILD / "graph_staging.sqlite"
    edge_digest = hashlib.sha256()
    g = open_db(staging)
    for row in g.execute("select src,dst,edge_type,weight from edges order by src,dst,edge_type"):
        edge_digest.update(repr(row).encode())
    counts = {t: n for t, n in g.execute("select node_type, count(*) from nodes group by 1")}
    ecounts = {t: n for t, n in g.execute("select edge_type, count(*) from edges group by 1")}
    g.close()
    snap_id = f"g{time.strftime('%Y%m%d')}-{edge_digest.hexdigest()[:12]}"
    dest = SNAPSHOTS / snap_id
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(staging, dest / "graph.sqlite")
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BACKEND,
                             capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        rev = "unknown"

    def src_hash(p: Path) -> str:
        h = hashlib.sha256()
        st = p.stat()
        h.update(f"{p.name}:{st.st_size}:{int(st.st_mtime)}".encode())
        return h.hexdigest()[:16]

    manifest = {
        "snapshot_id": snap_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "compiler_git_rev": rev,
        "edge_digest": edge_digest.hexdigest(),
        "sources": {p.name: src_hash(p) for p in
                    (SKILLS_DB, ENRICH_DB, BUILD / "registry.sqlite",
                     BUILD / "judgments_agg.sqlite", BUILD / "extractions.sqlite")},
        "node_counts": counts, "edge_counts": ecounts,
        "thresholds": {"cap_min": CAP_MIN_SKILLS, "tool_min": TOOL_MIN_SKILLS,
                       "artifact_min": ARTIFACT_MIN_SKILLS, "subst_min_shared": SUBST_MIN_SHARED,
                       "subst_max_df": SUBST_MAX_DF, "intent_min_cooc": INTENT_MIN_COOC,
                       "intent_min_pmi": INTENT_MIN_PMI},
        "quality_rule": "status=ok verdicts are the only positive evidence; "
                        "failed calls are retry_evidence and never strengthen edges",
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1))
    for f in dest.iterdir():
        f.chmod(0o444)
    dest.chmod(0o555)
    (SNAPSHOTS / "LATEST").unlink(missing_ok=True)
    (SNAPSHOTS / "LATEST").symlink_to(snap_id)
    print(f"  snapshot: {snap_id}\n  nodes {counts}\n  edges {ecounts}")
    mark_done("snapshot", {"snapshot_id": snap_id})


STAGES = [("registry", stage_registry), ("judgments", stage_judgments),
          ("extract", stage_extract), ("assemble", stage_assemble),
          ("snapshot", stage_snapshot)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=["all"] + [n for n, _ in STAGES] + ["force-assemble"])
    a = ap.parse_args()
    BUILD.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    for name, fn in STAGES:
        if a.stage == "force-assemble" and name in ("assemble", "snapshot"):
            (BUILD / f".done_{name}").unlink(missing_ok=True)
        if a.stage not in ("all", "force-assemble") and a.stage != name:
            continue
        if a.stage in ("all", "force-assemble") and stage_done(name):
            print(f"[{name}] already done")
            continue
        print(f"[{name}] starting", flush=True)
        fn()
        print(f"[{name}] done ({time.time()-t0:.0f}s total)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
