#!/usr/bin/env python3
"""Ingestion run 1 / Phase 5 — two-judge enrichment.

Stages (each independently resumable; everything keyed by normalized content hash):

  fetch      resolve each sampled skill's entrypoint + file tree from its PUBLIC source,
             store content-addressed under backend/skills_library_v1/
  prefilter  free deterministic verdicts (no model call): missing/invalid frontmatter,
             empty or <200-char body, entrypoint absent, exact-hash duplicate
  primary    Luna judge via sealed `codex exec`; emits run1_secondary_queue.json
  secondary-ingest   load Haiku verdicts produced by the orchestrator's subagents
  combine    final labels + bounded closure fetch + summary

The Haiku judge is NOT invoked from here: the secondary judge is the orchestrator's own
subagent mechanism, which lives outside this process. `primary` writes the queue,
`secondary-ingest` reads the answers back. That split is why the stages exist.

Writes only: backend/enrichment_v1.db, backend/skills_library_v1/, backend/bench/run1_*.json
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
DB = BACKEND / "enrichment_v1.db"
LIB = BACKEND / "skills_library_v1"
SAMPLE = BENCH / "enrichment_sample_v1.json"
FETCH_CACHE = BENCH / "run1_fetch_cache.json"
SECONDARY_QUEUE = BENCH / "run1_secondary_queue.json"
COMBINED = BENCH / "run1_combined_v1.json"

RUN_ID = "run1-20260802"
PROMPT_VERSION = "v1"
PRIMARY_PROMPT = BENCH / "enrichment_prompt_v1.md"
SECONDARY_PROMPT = BENCH / "enrichment_prompt_v1_secondary.md"

# --- judge pinning -----------------------------------------------------------
CODEX_BIN = "/home/sami/discord_codex/node_modules/.bin/codex"
CODEX_HOME = "/srv/mobile-codex/codex-home"
LUNA_MODEL = "gpt-5.6-luna"
LUNA_EFFORT = "medium"
LUNA_SNAPSHOT = f"{LUNA_MODEL}@{LUNA_EFFORT}/codex-cli-0.144.6"
HAIKU_SNAPSHOT = "claude-haiku-4-5-20251001"

# --- caps (ground rule 5) ----------------------------------------------------
MAX_LUNA_CALLS = 250
MAX_HAIKU_CALLS = 150
CONCURRENCY = 3

ENTRY_CHAR_CAP = 24_000
MIN_BODY_CHARS = 200
CLOSURE_MAX_FILES = 20
CLOSURE_MAX_BYTES = 256 * 1024
MAX_SUBDIRS = 4
LUNA_TIMEOUT = 420


# =============================================================== small helpers

def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(text: str) -> str:
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    return t.strip()


def norm_hash_of(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def store_object(content: bytes) -> str:
    h = hashlib.sha256(content).hexdigest()
    p = LIB / "objects" / h[:2] / h[2:4] / h
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, p)
    return h


def read_object(h: str) -> bytes | None:
    p = LIB / "objects" / h[:2] / h[2:4] / h
    return p.read_bytes() if p.exists() else None


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def record_enrichment(con, norm_hash, skill_id, skill_url, judge_role, model_snapshot,
                      output: dict, tokens_in, tokens_out, status) -> None:
    con.execute(
        "INSERT OR REPLACE INTO enrichments"
        " (norm_hash,skill_id,skill_url,judge_role,prompt_version,model_snapshot,"
        "  output_json,tokens_in,tokens_out,status,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (norm_hash, skill_id, skill_url, judge_role, PROMPT_VERSION, model_snapshot,
         json.dumps(output, ensure_ascii=False), tokens_in, tokens_out, status, now()))
    con.commit()


def already_judged(con, norm_hash: str, role: str, snapshot: str) -> dict | None:
    r = con.execute(
        "SELECT output_json,status FROM enrichments WHERE norm_hash=? AND judge_role=?"
        " AND prompt_version=? AND model_snapshot=?",
        (norm_hash, role, PROMPT_VERSION, snapshot)).fetchone()
    if not r:
        return None
    try:
        return {"output": json.loads(r[0]), "status": r[1]}
    except Exception:
        return {"output": {}, "status": r[1]}


# ==================================================================== fetching

def github_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    if hosts.exists():
        m = re.search(r"oauth_token:\s*(\S+)", hosts.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    raise SystemExit("no GitHub token available")


def api(url: str, token: str) -> tuple[int, object]:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "autoskill-run1-enrich",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as f:
                return f.status, json.loads(f.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 3:
                time.sleep(min(90, 12 * (2 ** attempt)))
                continue
            return e.code, {}
        except Exception:
            if attempt == 3:
                return -1, {}
            time.sleep(4)
    return -1, {}


def list_dir(repo: str, path: str, token: str) -> list[dict]:
    st, body = api(f"https://api.github.com/repos/{repo}/contents/"
                   + urllib.parse.quote(path.strip("/")), token)
    return body if st == 200 and isinstance(body, list) else []


def fetch_file(repo: str, path: str, token: str) -> bytes | None:
    st, body = api(f"https://api.github.com/repos/{repo}/contents/"
                   + urllib.parse.quote(path), token)
    if st != 200 or not isinstance(body, dict):
        return None
    if body.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(body.get("content") or "")
    except Exception:
        return None


def build_tree(repo: str, skill_dir: str, token: str) -> list[dict]:
    """Skill dir listing plus one level of subdirectories (bounded)."""
    tree: list[dict] = []
    top = list_dir(repo, skill_dir, token)
    subdirs = []
    for e in top:
        tree.append({"path": e.get("path"), "type": e.get("type"), "size": e.get("size") or 0})
        if e.get("type") == "dir":
            subdirs.append(e.get("path"))
    for sd in subdirs[:MAX_SUBDIRS]:
        for e in list_dir(repo, sd, token):
            tree.append({"path": e.get("path"), "type": e.get("type"),
                         "size": e.get("size") or 0})
    return tree


def resolve_entrypoint(skill: dict, token: str) -> dict:
    """Return {status, repo, entry_path, entry_hash, tree, detail}."""
    ref = skill["entrypoint_ref"]
    mode = ref.get("mode")
    if mode == "repo_path":
        repo, path = ref["repo"], ref["path"]
    elif mode == "repo_dir":
        repo, d = ref["repo"], ref["dir"]
        entries = list_dir(repo, d, token)
        cand = [e for e in entries
                if (e.get("name") or "").casefold() in ("skill.md", "agents.md", "readme.md")]
        cand.sort(key=lambda e: {"skill.md": 0, "agents.md": 1, "readme.md": 2}
                  .get((e.get("name") or "").casefold(), 9))
        if not cand:
            return {"status": "entrypoint_absent", "detail": f"no SKILL.md under {d}",
                    "repo": repo}
        path = cand[0]["path"]
    else:
        return {"status": "entrypoint_absent",
                "detail": f"corpus row has no entrypoint path (mode={mode})",
                "repo": ref.get("repo")}

    content = fetch_file(repo, path, token)
    if content is None:
        return {"status": "fetch_failed", "detail": f"could not fetch {repo}:{path}",
                "repo": repo, "entry_path": path}
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return {"status": "binary_entrypoint", "detail": "entrypoint is not UTF-8 text",
                "repo": repo, "entry_path": path}
    h = store_object(content)
    skill_dir = str(Path(path).parent).replace("\\", "/")
    tree = build_tree(repo, skill_dir, token)
    return {"status": "ok", "repo": repo, "entry_path": path, "entry_hash": h,
            "skill_dir": skill_dir, "tree": tree, "chars": len(text)}


def stage_fetch(sample: dict, token: str, limit: int | None) -> dict:
    cache = json.loads(FETCH_CACHE.read_text()) if FETCH_CACHE.exists() else {}
    skills = sample["skills"][:limit] if limit else sample["skills"]
    todo = [s for s in skills if s["id"] not in cache]
    print(f"fetch: {len(cache)} cached, {len(todo)} to fetch")
    done = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(resolve_entrypoint, s, token): s for s in todo}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                cache[s["id"]] = fut.result()
            except Exception as e:
                cache[s["id"]] = {"status": "fetch_failed", "detail": f"exception: {e}"}
            done += 1
            if done % 20 == 0:
                FETCH_CACHE.write_text(json.dumps(cache, indent=1))
                print(f"  fetched {done}/{len(todo)}", flush=True)
    FETCH_CACHE.write_text(json.dumps(cache, indent=1))
    stats: dict[str, int] = {}
    for v in cache.values():
        stats[v["status"]] = stats.get(v["status"], 0) + 1
    print("fetch statuses:", json.dumps(stats))
    return cache


# ================================================================= prefilters

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", re.S)


def split_frontmatter(text: str) -> tuple[dict | None, str]:
    m = FRONTMATTER_RE.match(text.lstrip("﻿"))
    if not m:
        return None, text
    block, body = m.group(1), text[m.end():]
    fm: dict[str, str] = {}
    for line in block.split("\n"):
        mm = re.match(r"\s*([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if mm:
            fm[mm.group(1).strip().casefold()] = mm.group(2).strip().strip("\"'")
    return fm, body


def prefilter(skill: dict, fetched: dict, seen_hashes: dict) -> dict | None:
    """Return a deterministic verdict dict, or None to send it to the judges."""
    st = fetched.get("status")
    if st != "ok":
        return {"is_real_skill": False, "confidence": 1.0, "vendor_convention": "unknown",
                "closure_paths": [], "summary": "", "triggers": [], "risk_flags": [],
                "reject_reason": {"entrypoint_absent": "entrypoint absent",
                                  "fetch_failed": "entrypoint could not be fetched from source",
                                  "binary_entrypoint": "entrypoint is not text"}
                .get(st, st),
                "_rule": st, "_detail": fetched.get("detail")}

    content = read_object(fetched["entry_hash"])
    text = content.decode("utf-8", "replace") if content else ""
    nh = norm_hash_of(text)

    if nh in seen_hashes and seen_hashes[nh] != skill["id"]:
        return {"is_real_skill": False, "confidence": 1.0, "vendor_convention": "unknown",
                "closure_paths": [], "summary": "", "triggers": [], "risk_flags": [],
                "reject_reason": "exact-hash duplicate of an already-judged skill",
                "_rule": "duplicate_hash", "_detail": f"same norm_hash as {seen_hashes[nh]}"}

    fm, body = split_frontmatter(text)
    if fm is None:
        return {"is_real_skill": False, "confidence": 1.0, "vendor_convention": "unknown",
                "closure_paths": [], "summary": "", "triggers": [], "risk_flags": [],
                "reject_reason": "missing YAML frontmatter",
                "_rule": "frontmatter_missing", "_detail": None}
    if not fm.get("name") and not fm.get("title"):
        return {"is_real_skill": False, "confidence": 1.0, "vendor_convention": "unknown",
                "closure_paths": [], "summary": "", "triggers": [], "risk_flags": [],
                "reject_reason": "frontmatter has no name/title",
                "_rule": "frontmatter_invalid", "_detail": sorted(fm.keys())[:8]}
    if len(body.strip()) < MIN_BODY_CHARS:
        return {"is_real_skill": False, "confidence": 1.0, "vendor_convention": "unknown",
                "closure_paths": [], "summary": "", "triggers": [], "risk_flags": [],
                "reject_reason": f"body under {MIN_BODY_CHARS} chars",
                "_rule": "body_too_short", "_detail": len(body.strip())}
    return None


# ===================================================================== judges

def build_judge_input(skill: dict, fetched: dict) -> tuple[str, str, bool]:
    content = read_object(fetched["entry_hash"]) or b""
    text = content.decode("utf-8", "replace")
    truncated = len(text) > ENTRY_CHAR_CAP
    shown = text[:ENTRY_CHAR_CAP]
    tree = fetched.get("tree") or []
    tree_lines = "\n".join(
        f"  {e['path']}  ({e.get('type')}, {e.get('size', 0)} bytes)" for e in tree[:200]
    ) or "  (no sibling files listed)"
    block = (
        "<<<UNTRUSTED_SKILL_DATA>>>\n"
        f"REPO: {fetched.get('repo')}\n"
        f"ENTRYPOINT PATH: {fetched.get('entry_path')}\n"
        f"TRUNCATED: {'yes — content cut at %d chars' % ENTRY_CHAR_CAP if truncated else 'no'}\n"
        "\nFILE TREE (the ONLY paths that may appear in closure_paths):\n"
        f"{tree_lines}\n"
        "\nENTRYPOINT CONTENT:\n"
        "-----8<----- BEGIN CONTENT -----8<-----\n"
        f"{shown}\n"
        "-----8<----- END CONTENT -----8<-----\n"
        "<<<END_UNTRUSTED_SKILL_DATA>>>\n"
    )
    return block, norm_hash_of(text), truncated


def file_paths(fetched: dict) -> set[str]:
    """Only regular files are legal closure targets — a directory is not a dependency."""
    return {e["path"] for e in (fetched.get("tree") or [])
            if e.get("path") and e.get("type") == "file"}


def parse_judge_json(raw: str) -> dict | None:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"\A```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*\Z", "", s).strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    depth, start = 0, None
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    v = json.loads(s[start:i + 1])
                    if isinstance(v, dict):
                        return v
                except Exception:
                    pass
                start = None
    return None


def validate_output(out: dict, tree_paths: set[str]) -> tuple[dict, list[str]]:
    flags: list[str] = []
    clean: dict = {}
    clean["is_real_skill"] = bool(out.get("is_real_skill"))
    try:
        c = float(out.get("confidence", 0.0))
    except Exception:
        c = 0.0
        flags.append("confidence_unparseable")
    clean["confidence"] = min(1.0, max(0.0, c))
    vc = str(out.get("vendor_convention") or "unknown").strip().casefold()
    if vc not in ("claude", "codex", "cursor", "copilot", "generic", "unknown"):
        flags.append("vendor_convention_invalid")
        vc = "unknown"
    clean["vendor_convention"] = vc
    raw_paths = out.get("closure_paths") or []
    if not isinstance(raw_paths, list):
        raw_paths, = ([],)
        flags.append("closure_paths_not_list")
    kept, violations = [], []
    for p in raw_paths:
        p = str(p).strip()
        if p in tree_paths:
            kept.append(p)
        else:
            violations.append(p)
    if violations:
        flags.append("path_violation")
    clean["closure_paths"] = kept[:CLOSURE_MAX_FILES]
    clean["closure_path_violations"] = violations[:20]
    clean["summary"] = str(out.get("summary") or "")[:600]
    tr = out.get("triggers") or []
    clean["triggers"] = [str(t)[:160] for t in tr][:8] if isinstance(tr, list) else []
    rf = out.get("risk_flags") or []
    clean["risk_flags"] = [str(x)[:60] for x in rf][:12] if isinstance(rf, list) else []
    rr = out.get("reject_reason")
    clean["reject_reason"] = (str(rr)[:300] if rr not in (None, "", "null") else None)
    clean["model_self_report"] = str(out.get("model_self_report") or "")[:120]
    clean["_validation_flags"] = flags
    return clean, flags


def call_luna(prompt_text: str) -> dict:
    """One sealed codex exec call. Empty temp cwd, scrubbed env, read-only sandbox."""
    jail = tempfile.mkdtemp(prefix="luna-judge-")
    outfile = Path(jail) / "_last.txt"
    env = {"HOME": "/home/sami", "PATH": "/usr/bin:/bin",
           "CODEX_HOME": CODEX_HOME, "TERM": "dumb"}
    cmd = [CODEX_BIN, "exec", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
           "-s", "read-only", "-C", jail, "-m", LUNA_MODEL,
           "-c", f'model_reasoning_effort="{LUNA_EFFORT}"',
           "--json", "-o", str(outfile), "-"]
    try:
        p = subprocess.run(cmd, input=prompt_text, env=env, capture_output=True,
                           text=True, timeout=LUNA_TIMEOUT)
        tin = tout = 0
        err = None
        for line in (p.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type") == "turn.completed":
                u = ev.get("usage") or {}
                tin = u.get("input_tokens", 0) or 0
                tout = u.get("output_tokens", 0) or 0
            elif ev.get("type") in ("error", "turn.failed"):
                err = str(ev.get("message") or ev.get("error"))[:300]
        text = outfile.read_text(encoding="utf-8") if outfile.exists() else ""
        return {"text": text, "tokens_in": tin, "tokens_out": tout,
                "error": err, "returncode": p.returncode}
    except subprocess.TimeoutExpired:
        return {"text": "", "tokens_in": 0, "tokens_out": 0,
                "error": f"timeout after {LUNA_TIMEOUT}s", "returncode": -9}
    finally:
        shutil.rmtree(jail, ignore_errors=True)


def stage_primary(sample: dict, cache: dict, limit: int | None) -> dict:
    con = db()
    prompt = PRIMARY_PROMPT.read_text(encoding="utf-8")
    skills = sample["skills"][:limit] if limit else sample["skills"]

    seen_hashes: dict[str, str] = {}
    pre_rows, judge_rows = [], []
    for s in skills:
        f = cache.get(s["id"], {"status": "fetch_failed", "detail": "not fetched"})
        v = prefilter(s, f, seen_hashes)
        if f.get("status") == "ok":
            content = read_object(f["entry_hash"])
            if content:
                nh = norm_hash_of(content.decode("utf-8", "replace"))
                seen_hashes.setdefault(nh, s["id"])
        if v is not None:
            pre_rows.append((s, f, v))
        else:
            judge_rows.append((s, f))

    print(f"prefilter: {len(pre_rows)} deterministic verdicts, {len(judge_rows)} to Luna")
    for s, f, v in pre_rows:
        content = read_object(f["entry_hash"]) if f.get("status") == "ok" else None
        nh = norm_hash_of(content.decode("utf-8", "replace")) if content else \
            "nofetch:" + hashlib.sha256(s["id"].encode()).hexdigest()[:56]
        record_enrichment(con, nh, s["id"], s.get("url"), "deterministic", "rule-engine-v1",
                          v, 0, 0, "ok")

    # ---- Luna, concurrency-capped, resumable by norm_hash --------------------
    todo = []
    for s, f in judge_rows:
        block, nh, trunc = build_judge_input(s, f)
        if already_judged(con, nh, "primary", LUNA_SNAPSHOT):
            continue
        todo.append((s, f, block, nh, trunc))
    print(f"luna: {len(judge_rows) - len(todo)} already judged, {len(todo)} to call")
    if len(todo) > MAX_LUNA_CALLS:
        print(f"  CAP: trimming {len(todo)} -> {MAX_LUNA_CALLS} Luna calls")
        todo = todo[:MAX_LUNA_CALLS]

    calls = {"made": 0, "retried": 0, "ok": 0, "malformed": 0, "failed": 0,
             "tokens_in": 0, "tokens_out": 0}

    def work(item):
        s, f, block, nh, trunc = item
        full = prompt + "\n\n" + block
        r = call_luna(full)
        parsed = parse_judge_json(r["text"])
        retried = False
        if parsed is None and not r.get("error"):
            retried = True
            r2 = call_luna(full + "\n\nYour previous reply was not parseable. "
                                  "Return ONE JSON object and nothing else.")
            r["tokens_in"] += r2["tokens_in"]
            r["tokens_out"] += r2["tokens_out"]
            parsed = parse_judge_json(r2["text"])
            if parsed is not None:
                r["text"] = r2["text"]
            elif r2.get("error"):
                r["error"] = r2["error"]
        return s, f, nh, trunc, r, parsed, retried

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = [ex.submit(work, it) for it in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            s, f, nh, trunc, r, parsed, retried = fut.result()
            calls["made"] += 1
            calls["retried"] += int(retried)
            calls["tokens_in"] += r["tokens_in"]
            calls["tokens_out"] += r["tokens_out"]
            tree_paths = file_paths(f)
            if parsed is None:
                status = "failed(%s)" % (r.get("error") or "no_json")[:60] if r.get("error") \
                    else "malformed"
                calls["failed" if r.get("error") else "malformed"] += 1
                record_enrichment(con, nh, s["id"], s.get("url"), "primary", LUNA_SNAPSHOT,
                                  {"raw_text": (r["text"] or "")[:2000],
                                   "error": r.get("error"), "truncated_input": trunc},
                                  r["tokens_in"], r["tokens_out"], status)
            else:
                clean, flags = validate_output(parsed, tree_paths)
                clean["truncated_input"] = trunc
                calls["ok"] += 1
                record_enrichment(con, nh, s["id"], s.get("url"), "primary", LUNA_SNAPSHOT,
                                  clean, r["tokens_in"], r["tokens_out"], "ok")
            if i % 5 == 0 or i == len(futs):
                print(f"  luna {i}/{len(futs)}  ok={calls['ok']} malformed={calls['malformed']}"
                      f" failed={calls['failed']} tok_in={calls['tokens_in']}", flush=True)

    # ---- build the secondary queue ------------------------------------------
    queue = []
    for s, f in judge_rows:
        block, nh, trunc = build_judge_input(s, f)
        prim = already_judged(con, nh, "primary", LUNA_SNAPSHOT)
        if not prim:
            continue
        o = prim["output"]
        needs = (prim["status"] != "ok") or (not o.get("is_real_skill")) \
            or (float(o.get("confidence") or 0.0) < 0.6)
        if not needs:
            continue
        if already_judged(con, nh, "secondary", HAIKU_SNAPSHOT):
            continue
        queue.append({"skill_id": s["id"], "name": s.get("name"), "url": s.get("url"),
                      "norm_hash": nh, "data_block": block,
                      "tree_paths": sorted(file_paths(f)),
                      "primary_status": prim["status"],
                      "primary_is_real": o.get("is_real_skill"),
                      "primary_confidence": o.get("confidence")})
    if len(queue) > MAX_HAIKU_CALLS:
        print(f"  CAP: trimming secondary queue {len(queue)} -> {MAX_HAIKU_CALLS}")
        queue = queue[:MAX_HAIKU_CALLS]
    SECONDARY_QUEUE.write_text(json.dumps(
        {"run_id": RUN_ID, "prompt_version": PROMPT_VERSION,
         "model_snapshot": HAIKU_SNAPSHOT, "created_at": now(),
         "count": len(queue), "items": queue}, indent=1), encoding="utf-8")
    print(f"luna calls: {json.dumps(calls)}")
    print(f"secondary queue: {len(queue)} items -> {SECONDARY_QUEUE}")
    con.close()
    return calls


def stage_secondary_ingest(results_path: Path) -> dict:
    con = db()
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    queue = json.loads(SECONDARY_QUEUE.read_text(encoding="utf-8"))
    by_hash = {i["norm_hash"]: i for i in queue["items"]}
    stats = {"ok": 0, "malformed": 0, "unknown_hash": 0}
    for item in payload.get("results", []):
        nh = item.get("norm_hash")
        q = by_hash.get(nh)
        if not q:
            stats["unknown_hash"] += 1
            continue
        parsed = item.get("output") if isinstance(item.get("output"), dict) else \
            parse_judge_json(item.get("raw", ""))
        if parsed is None:
            stats["malformed"] += 1
            record_enrichment(con, nh, q["skill_id"], q["url"], "secondary", HAIKU_SNAPSHOT,
                              {"raw_text": str(item.get("raw"))[:2000]}, 0, 0, "malformed")
            continue
        clean, _ = validate_output(parsed, set(q["tree_paths"]))
        record_enrichment(con, nh, q["skill_id"], q["url"], "secondary", HAIKU_SNAPSHOT,
                          clean, item.get("tokens_in", 0), item.get("tokens_out", 0), "ok")
        stats["ok"] += 1
    con.close()
    print("secondary ingest:", json.dumps(stats))
    return stats


# ==================================================================== combine

def fetch_closure(repo: str, paths: list[str], token: str) -> dict:
    got, missing, present = [], [], []
    for p in paths[:CLOSURE_MAX_FILES]:
        content = fetch_file(repo, p, token)
        if content is None:
            missing.append(p)
            continue
        if len(content) > CLOSURE_MAX_BYTES:
            missing.append(p + f" (>{CLOSURE_MAX_BYTES}B)")
            continue
        h = hashlib.sha256(content).hexdigest()
        if (LIB / "objects" / h[:2] / h[2:4] / h).exists():
            present.append(p)
        else:
            store_object(content)
            got.append(p)
    return {"fetched": got, "already_present": present, "missing": missing}


def stage_combine(sample: dict, cache: dict, token: str, do_closure: bool) -> dict:
    con = db()
    rows = []
    canary_ids = {s["id"] for s in sample["skills"] if "canary" in s["buckets"]}
    closure_stats = {"skills_with_closure": 0, "fetched": 0, "already_present": 0, "missing": 0}

    for s in sample["skills"]:
        f = cache.get(s["id"], {})
        if f.get("status") == "ok":
            c = read_object(f["entry_hash"])
            nh = norm_hash_of(c.decode("utf-8", "replace")) if c else None
        else:
            nh = "nofetch:" + hashlib.sha256(s["id"].encode()).hexdigest()[:56]
        det = already_judged(con, nh, "deterministic", "rule-engine-v1") if nh else None
        prim = already_judged(con, nh, "primary", LUNA_SNAPSHOT) if nh else None
        sec = already_judged(con, nh, "secondary", HAIKU_SNAPSHOT) if nh else None

        rec = {"skill_id": s["id"], "name": s.get("name"), "url": s.get("url"),
               "source": s.get("source"), "buckets": s["buckets"], "norm_hash": nh,
               "is_canary": s["id"] in canary_ids,
               "fetch_status": f.get("status"), "repo": f.get("repo"),
               "entry_path": f.get("entry_path")}

        if det:
            rec["label"] = "excluded_junk"
            rec["decided_by"] = "deterministic"
            rec["reason"] = det["output"].get("reject_reason")
            rec["rule"] = det["output"].get("_rule")
        elif prim is None:
            rec["label"] = "pending"
            rec["decided_by"] = None
            rec["reason"] = "no primary verdict recorded"
        elif prim["status"] != "ok":
            rec["label"] = "malformed" if prim["status"] == "malformed" else "pending"
            rec["decided_by"] = "primary"
            rec["reason"] = prim["status"]
            if sec and sec["status"] == "ok":
                rec["label"] = "quarantine"
                rec["reason"] = f"primary {prim['status']}; secondary responded"
                rec["secondary"] = {k: sec["output"].get(k)
                                    for k in ("is_real_skill", "confidence", "reject_reason")}
        else:
            po = prim["output"]
            p_keep = bool(po.get("is_real_skill"))
            rec["primary"] = {"is_real_skill": p_keep,
                              "confidence": po.get("confidence"),
                              "vendor_convention": po.get("vendor_convention"),
                              "summary": po.get("summary"),
                              "triggers": po.get("triggers"),
                              "risk_flags": po.get("risk_flags"),
                              "reject_reason": po.get("reject_reason"),
                              "closure_paths": po.get("closure_paths"),
                              "path_violations": po.get("closure_path_violations"),
                              "validation_flags": po.get("_validation_flags"),
                              "truncated_input": po.get("truncated_input"),
                              "model_self_report": po.get("model_self_report")}
            low_conf = float(po.get("confidence") or 0.0) < 0.6
            if not p_keep or low_conf:
                if sec is None:
                    rec["label"] = "pending"
                    rec["decided_by"] = "primary"
                    rec["reason"] = ("secondary review required but not returned"
                                     + (" (low confidence)" if (p_keep and low_conf) else ""))
                elif sec["status"] != "ok":
                    rec["label"] = "quarantine"
                    rec["decided_by"] = "both"
                    rec["reason"] = f"secondary {sec['status']}"
                else:
                    so = sec["output"]
                    s_keep = bool(so.get("is_real_skill"))
                    rec["secondary"] = {"is_real_skill": s_keep,
                                        "confidence": so.get("confidence"),
                                        "reject_reason": so.get("reject_reason"),
                                        "summary": so.get("summary"),
                                        "closure_paths": so.get("closure_paths"),
                                        "model_self_report": so.get("model_self_report")}
                    rec["decided_by"] = "both"
                    if not p_keep and not s_keep:
                        rec["label"] = "excluded_junk"
                        rec["reason"] = po.get("reject_reason") or so.get("reject_reason")
                    elif p_keep and s_keep:
                        rec["label"] = "included"
                        rec["reason"] = "both keep (primary confidence < 0.6)"
                    else:
                        rec["label"] = "quarantine"
                        rec["reason"] = ("judges disagree: primary "
                                         f"{'keep' if p_keep else 'reject'}, "
                                         f"secondary {'keep' if s_keep else 'reject'}")
            else:
                rec["label"] = "included"
                rec["decided_by"] = "primary"
                rec["reason"] = "primary keeps with confidence >= 0.6"

        # bounded closure fetch for anything kept
        if do_closure and rec["label"] in ("included", "quarantine") and f.get("repo"):
            paths = (rec.get("primary") or {}).get("closure_paths") or []
            if paths:
                cf = fetch_closure(f["repo"], paths, token)
                rec["closure"] = cf
                closure_stats["skills_with_closure"] += 1
                closure_stats["fetched"] += len(cf["fetched"])
                closure_stats["already_present"] += len(cf["already_present"])
                closure_stats["missing"] += len(cf["missing"])
        rows.append(rec)

    labels: dict[str, int] = {}
    for r in rows:
        labels[r["label"]] = labels.get(r["label"], 0) + 1

    canary_rows = [r for r in rows if r["is_canary"]]
    regressions = [r for r in canary_rows if r["label"] != "included"]

    out = {"run_id": RUN_ID, "generated_at": now(), "total": len(rows),
           "labels": labels, "closure_stats": closure_stats,
           "canaries": {"total": len(canary_rows),
                        "included": sum(1 for r in canary_rows if r["label"] == "included"),
                        "regressions": [{"skill_id": r["skill_id"], "name": r["name"],
                                         "label": r["label"], "reason": r["reason"]}
                                        for r in regressions]},
           "prompt_regression": bool(regressions),
           "rows": rows}
    COMBINED.write_text(json.dumps(out, indent=1), encoding="utf-8")
    con.close()
    print("labels:", json.dumps(labels))
    print("closure:", json.dumps(closure_stats))
    print(f"canaries included {out['canaries']['included']}/{out['canaries']['total']}")
    if regressions:
        print("PROMPT REGRESSION:", [r["name"] for r in regressions])
    print("->", COMBINED)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True,
                    choices=["fetch", "primary", "secondary-ingest", "combine", "all"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--results", type=Path)
    ap.add_argument("--no-closure", action="store_true")
    args = ap.parse_args()

    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    token = github_token()
    LIB.mkdir(parents=True, exist_ok=True)

    if args.stage in ("fetch", "all"):
        stage_fetch(sample, token, args.limit)
    cache = json.loads(FETCH_CACHE.read_text()) if FETCH_CACHE.exists() else {}
    if args.stage in ("primary", "all"):
        stage_primary(sample, cache, args.limit)
    if args.stage == "secondary-ingest":
        if not args.results:
            raise SystemExit("--results required")
        stage_secondary_ingest(args.results)
    if args.stage in ("combine", "all"):
        stage_combine(sample, cache, token, not args.no_closure)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
