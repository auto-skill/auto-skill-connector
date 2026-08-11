#!/usr/bin/env python3
"""Ingestion run 2 — two-judge enrichment harness (v2).

Derived from run1_enrich.py. Changes in v2:
  * build_tree()/resolve_entrypoint() resolve SYMLINKED entrypoints (git mode 120000,
    detected via listed-size vs fetched-size mismatch) and judge the TARGET with the
    TARGET's directory tree, recording `entrypoint_symlink` provenance.
  * prompt v2 redefines prompt_injection and adds advisory `specificity`.
  * confidence is recorded but REMOVED FROM ALL LOGIC: escalation to the secondary judge
    now happens on a primary reject only.

Original run-1 docstring follows.

Ingestion run 1 / Phase 5 — two-judge enrichment.

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
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

# Backstop: no socket may block forever. A hung read is what stalled a stage
# for 22 minutes with the API quota completely unused.
socket.setdefaulttimeout(60)

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
DB = BACKEND / "enrichment_v1.db"
LIB = BACKEND / "skills_library_v1"
SAMPLE = BENCH / os.environ.get("RUN2_SAMPLE", "enrichment_sample_v1.json")
FETCH_CACHE = BENCH / os.environ.get("RUN2_FETCH_CACHE", "run2_fetch_cache.json")
SECONDARY_QUEUE = BENCH / os.environ.get("RUN2_SECONDARY_QUEUE", "run2_secondary_queue.json")
COMBINED = BENCH / os.environ.get("RUN2_COMBINED", "run2_combined_v2.json")

RUN_ID = os.environ.get("RUN2_RUN_ID", "run2-20260802")
PROMPT_VERSION = os.environ.get("RUN2_PROMPT_VERSION", "v2")
PRIMARY_PROMPT = BENCH / "enrichment_prompt_v2.md"
SECONDARY_PROMPT = BENCH / "enrichment_prompt_v2_secondary.md"

# --- judge pinning -----------------------------------------------------------
CODEX_BIN = "/home/sami/discord_codex/node_modules/.bin/codex"
CODEX_HOME = "/srv/mobile-codex/codex-home"
LUNA_MODEL = "gpt-5.6-luna"
# Reasoning effort is an operational lever, not a constant: it is the single
# biggest control on judging latency. The API accepts none/low/medium/high/
# xhigh/max ("minimal" is rejected by this model). The snapshot string embeds it,
# so verdicts stay attributable to the effort that produced them and a later
# calibration can compare across levels the same way judge_calibration.json does
# for the Haiku failover. Changing this does NOT trigger a mass re-judge:
# run2_build_batch.judged_ids() keys on prompt_version, not model_snapshot.
LUNA_EFFORT = os.environ.get("AUTOSKILL_LUNA_EFFORT", "medium")
_VALID_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}
if LUNA_EFFORT not in _VALID_EFFORTS:
    raise SystemExit(
        f"AUTOSKILL_LUNA_EFFORT={LUNA_EFFORT!r} is not accepted by {LUNA_MODEL}; "
        f"valid: {sorted(_VALID_EFFORTS)}. Failing loudly rather than silently "
        f"judging a whole batch at an unintended effort.")
LUNA_SNAPSHOT = f"{LUNA_MODEL}@{LUNA_EFFORT}/codex-cli-0.144.6"
# "both" claims two INDEPENDENT judges concurred. During Luna failover the
# primary is Haiku and the secondary is also Haiku, so a concurrence is two
# samples of one model, not two judges. Label it for what it is so nobody
# later reads this window as double-confirmed.
def concur_label() -> str:
    return "both_same_model" if JUDGE_FAILOVER else "both"


# Infrastructure failures vs content failures. The distinction decides whether a
# non-answer becomes a retry or a verdict, and getting it wrong has now cost the
# corpus twice: transient 403s once turned into permanent `excluded_junk`, and a
# judge usage limit turned into 56 quarantine holds. Anything that means "the
# judge never got to read this" is retryable and says nothing about the skill.
INFRA_FAIL_MARKERS = ("usage limit", "rate limit", "timeout", "timed out",
                      "connection", "network", "unreachable", "429", "503", "502",
                      "overloaded", "weekly limit", "rc=", "returncode", "exception")
SECONDARY_UNAVAILABLE_MARKERS = (
    "oauth session expired and could not be refreshed",
    "you've hit your weekly limit",
    "you've hit your usage limit",
)


def infra_failure(status: str) -> bool:
    s = (status or "").lower()
    return any(m in s for m in INFRA_FAIL_MARKERS)


def primary_snapshot() -> str:
    """Never label a failover verdict as a Luna verdict."""
    return FAILOVER_SNAPSHOT if JUDGE_FAILOVER else LUNA_SNAPSHOT
HAIKU_SNAPSHOT = "claude-haiku-4-5-20251001"
# Judge failover. Luna hit a hard usage limit with a multi-day reset; without a
# fallback the corpus stops growing entirely for that whole window. Failover
# verdicts are tagged with their own snapshot so they are attributable and can
# be re-judged by Luna later -- they are NOT silently mixed in as Luna verdicts.
JUDGE_FAILOVER = os.environ.get("AUTOSKILL_JUDGE_FAILOVER", "") == "haiku"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/sami/.npm-global/bin/claude")
FAILOVER_SNAPSHOT = f"{HAIKU_SNAPSHOT}/primary-failover"

# --- caps (ground rule 5) ----------------------------------------------------
# Budget is in LUNA CALLS, not skills. With batching one call judges
# LUNA_BATCH_SIZE skills, so the skill budget is MAX_LUNA_CALLS * LUNA_BATCH_SIZE.
# The old code capped *skills* at 250 and silently dropped the remainder: at batch
# size 400 it trimmed 338 -> 250, took canaries with it, and tripped a canary
# regression STOP. Canaries are now never trimmed, and any trim is recorded.
MAX_LUNA_CALLS = int(os.environ.get("AUTOSKILL_MAX_LUNA_CALLS", "400"))
MAX_HAIKU_CALLS = 150
CONCURRENCY = int(os.environ.get("AUTOSKILL_CONCURRENCY", "3"))
# Fetch concurrency is NOT the Luna concurrency. They were the same knob, so
# throttling Luna also throttled fetching to 3 -- and fetching is now off-quota
# (raw.githubusercontent), where 20-way sustained 73 files/sec without throttling.
FETCH_CONCURRENCY = int(os.environ.get("AUTOSKILL_FETCH_CONCURRENCY", "16"))
# One pathological repo must not stall a batch. Observed: a single fetch hung a
# 222-skill stage for 22 minutes while the API quota sat completely unused, and
# individual resolutions measured 0.7s. Unfinished work is left retryable
# (-> pending), never recorded as a verdict about the skill.
FETCH_STAGE_DEADLINE = int(os.environ.get("AUTOSKILL_FETCH_DEADLINE", "900"))

ENTRY_CHAR_CAP = 24_000
MIN_BODY_CHARS = 200
# A skill's value to a model is mostly its references and scripts. Capping at 20
# files was silently truncating the richest skills -- slidev kept 20 of 52,
# diagram-design 20 of 65, claude-android-ninja 20 of 30 -- so the corpus held a
# fraction of exactly the material that would help on a real task.
# 100 still silently starved reference-library skills (vueuse-functions: 164
# reference docs, all skipped). The 8MB per-skill byte cap is the real guard;
# the file cap is only a sanity bound.
CLOSURE_MAX_FILES = int(os.environ.get("AUTOSKILL_CLOSURE_MAX_FILES", "400"))
from run2_repofetch import raw_url_fetch  # off-quota single-file fetch

CLOSURE_MAX_BYTES = 256 * 1024                     # per file
CLOSURE_MAX_TOTAL_BYTES = 8 * 1024 * 1024          # per skill, stops one repo blowing up the store

# Text a model can actually read and act on, ranked ahead of binary assets when
# the cap bites: a reference doc or script helps on a task, a hero.gif does not.
TEXTUAL_DEP_RE = re.compile(
    r"\.(md|markdown|txt|py|sh|bash|zsh|js|mjs|ts|tsx|jsx|json|ya?ml|toml|ini|cfg|"
    r"sql|rb|go|rs|java|kt|c|h|cpp|hpp|cs|php|pl|r|jl|tf|dockerfile|env|template|tmpl)$",
    re.IGNORECASE)
MAX_SUBDIRS = 4
# Flags that must BLOCK a skill from being served. license_missing is advisory
# metadata and deliberately excluded from this set.
# Only flags meaning THE ARTIFACT ITSELF IS HOSTILE block inclusion. The first
# cut also blocked credential_request / network_exfiltration / destructive_commands
# and immediately quarantined a canary ("HTML Injection Testing") -- those are
# SUBJECT-MATTER flags: a security skill discusses credentials, a deploy skill
# runs destructive commands. Blocking them would delete exactly the specialised
# skills the corpus exists to provide. They stay recorded for retrieval-time
# policy, they just do not gate storage.
# Measured over every firing: `malware_indicators` fired on 58 security-domain
# skills and 0 pieces of malware -- detection engineering, memory forensics,
# webshell hunting. It describes what a skill is ABOUT, not what it does to you,
# so it joins the topic set. What remains blocks only artifacts hostile to the
# reader: an injection aimed at the agent, or deliberately obfuscated payloads.
HARD_RISK_FLAGS = {"prompt_injection", "obfuscated_code"}
TOPIC_RISK_FLAGS = {"credential_request", "network_exfiltration",
                    "destructive_commands", "license_missing", "malware_indicators"}
# Eval corpora ship SKILL.md files that are test material, not usable skills.
# Anchored to ANCESTOR directories only. Matching the skill's own directory
# excluded real skills that happen to be named for what they teach --
# `.claude/skills/fixtures/SKILL.md` (Playwright fixture conventions) and
# `.claude/skills/test-cases/SKILL.md` (a pytest generator) are skills, not
# fixtures. A fixture is identified by what CONTAINS it.
FIXTURE_PATH_RE = re.compile(
    r"/(datasets?|fixtures?|test[-_]?cases?|testdata|examples?/cases?|"
    r"case_\d+|benchmarks?/cases?|__tests__|spec/fixtures)/(?=.*/)", re.IGNORECASE)
# raw.githubusercontent is off-quota and did not throttle at 20-way in
# measurement (73 files/sec, 125/125 ok). The old default of 3 existed only
# to avoid secondary-rate-limiting the contents API, which is no longer the
# primary path.
CLOSURE_CONCURRENCY = int(os.environ.get("AUTOSKILL_CLOSURE_CONCURRENCY", "64"))
# raw.githubusercontent and the authenticated contents API have DIFFERENT limits
# and must not share one concurrency number. Measured on this host: raw sustains
# 41-91 files/sec clean at 20-96 way, zero errors. The contents API trips
# GitHub's SECONDARY limiter when burst, and api() answers a 403 by sleeping
# 20s, then 40s, then 80s.
#
# Fanning closure out 20-way across skills therefore made the FALLBACK path
# 20-way too, and production closure collapsed to 2.1 files/sec -- 20-40x below
# what raw alone sustains -- because every miss queued behind those sleeps.
# This semaphore bounds ONLY the authenticated fallback, so the fast path can
# run wide while the rate-limited one stays at the 3-way it was always safe at.
_API_GATE = threading.Semaphore(int(os.environ.get("AUTOSKILL_API_CONCURRENCY", "3")))
LUNA_TIMEOUT = 420
# Judging several skills per Luna call. Measured: process spawn is only 254ms of a
# 7-8s call and the system prompt is already server-side cached, so a persistent
# session would save <1% AND would leak one skill's content into the next
# judgement. Batching is the win and stays bounded — one call, then a fresh
# context. 0 disables batching entirely.
LUNA_BATCH_SIZE = int(os.environ.get("AUTOSKILL_LUNA_BATCH", "8"))
BATCHED_PROMPT = BENCH / "enrichment_prompt_v2_batched.md"


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
    # Four processes now write this DB (enrich, treeharvest, deep-harvest,
    # inherit). WAL allows concurrent readers but still serialises writers, and
    # the harvesters commit in large batches -- long enough to blow a 60s
    # timeout and crash a judging run mid-batch, losing the rest of the batch's
    # Luna work. busy_timeout makes writers wait instead of erroring.
    con = sqlite3.connect(DB, timeout=300)
    con.execute("PRAGMA journal_mode=WAL")
    # synchronous=NORMAL is SQLite's recommended setting for WAL mode: still
    # corruption-safe and still durable against process crash, trading only the
    # last few transactions on machine power loss. Measured on this volume:
    # FULL = 244 ms/commit, NORMAL = 2 ms (122x). With six writers committing
    # continuously that fsync was a dominant, invisible cost.
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=300000")
    return con


def record_enrichment(con, norm_hash, skill_id, skill_url, judge_role, model_snapshot,
                      output: dict, tokens_in, tokens_out, status) -> None:
    """Persist one verdict. Retries on lock: a paid-for judgement must never be
    lost to contention with the harvesters."""
    for attempt in range(6):
        try:
            return _record_enrichment(con, norm_hash, skill_id, skill_url, judge_role,
                                      model_snapshot, output, tokens_in, tokens_out,
                                      status)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            time.sleep(5 * (attempt + 1))
    print(f"  WARN: could not persist verdict for {norm_hash[:12]} after retries",
          flush=True)


def _record_enrichment(con, norm_hash, skill_id, skill_url, judge_role, model_snapshot,
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
    """Return a prior verdict for this skill, or None if it still needs judging.

    A `failed(...)` row is NOT a verdict -- it is the record of a call that died
    in flight (quota exhaustion, stream disconnect, model at capacity). Those
    rows carry no `is_real_skill`, so treating them as "judged" silently drops
    the skill from the corpus forever: the caller skips it, and nothing ever
    re-offers it.

    Measured 2026-08-11: 9,408 primary rows had a failed status, only 302 ever
    recovered (by racing in under a different snapshot) -- 9,106 skills were
    permanently skipped for transient infra reasons that say nothing about the
    content. Every future mid-batch quota exhaustion would add more.

    Reporting them as unjudged is safe: `_record_enrichment` is INSERT OR
    REPLACE keyed on exactly this tuple, so a retry overwrites the failed row.
    """
    r = con.execute(
        "SELECT output_json,status FROM enrichments WHERE norm_hash=? AND judge_role=?"
        " AND prompt_version=? AND model_snapshot=?",
        (norm_hash, role, PROMPT_VERSION, snapshot)).fetchone()
    if not r:
        return None
    if (r[1] or "").startswith("failed"):
        return None
    try:
        return {"output": json.loads(r[0]), "status": r[1]}
    except Exception:
        return {"output": {}, "status": r[1]}


# ==================================================================== fetching

# ---- shared GitHub secondary-limit cooldown -------------------------------
# GitHub's SECONDARY rate limit is token-wide and independent of the hourly
# quota: measured 2026-08-07, actual calls returned 403 with
# X-RateLimit-Remaining: 0 while /rate_limit still reported core 0/5000 used.
# It cleared only after ~300s of TOTAL quiet across every unit. The old handlers
# slept 45s and retried, which re-tripped it immediately -- that oscillation is
# what produced the periodic batch failures (every 4th batch) and the
# "stage deadline exceeded" fetch losses.
#
# Because the limit covers the whole token, the backoff has to be shared: one
# unit tripping it blocks all of them, so they all have to stand down together.
GH_COOLDOWN = Path("/srv/mobile-codex/autoskill-daemon/state/gh_cooldown_until")
GH_COOLDOWN_SECONDS = 300


def gh_cooldown_remaining() -> float:
    try:
        return max(0.0, float(GH_COOLDOWN.read_text(encoding="utf-8")) - time.time())
    except Exception:
        return 0.0


def gh_trip_cooldown(seconds: int = GH_COOLDOWN_SECONDS) -> None:
    """Record a token-wide stand-down. Best effort: never raise into a fetch."""
    try:
        GH_COOLDOWN.parent.mkdir(parents=True, exist_ok=True)
        GH_COOLDOWN.write_text(str(time.time() + seconds), encoding="utf-8")
    except Exception:
        pass


def gh_wait_cooldown(label: str = "") -> None:
    rem = gh_cooldown_remaining()
    if rem > 0:
        print(f"  github secondary-limit cooldown: waiting {int(rem)}s {label}", flush=True)
        time.sleep(min(rem, GH_COOLDOWN_SECONDS))


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
    # In-band retry budget is deliberately small. A worker held here is held
    # against FETCH_STAGE_DEADLINE, which ~822 skills share; the old ladder
    # (4 attempts, 20/40/80s sleeps, 120s cap, 60s timeouts) let ONE pathological
    # skill occupy a worker for ~380s. With 8 workers that meant a handful of
    # 403-prone repos could stall the whole stage past 900s -- measured as 372 of
    # 390 fetch failures being "stage deadline exceeded", not GitHub refusals,
    # and a bimodal profile (median 97s, but 24% of batches blowing the deadline
    # outright). Retrying here trades a scarce resource for a plentiful one:
    # `fetch_failed` is in RETRYABLE, so run2_build_batch re-offers the row in the
    # next batch at no cost to this one. Yield the worker early instead.
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=30) as f:
                return f.status, json.loads(f.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                # remaining=0 on a 403 is the SECONDARY limit, not the hourly
                # quota. It is token-wide, so record it for every unit rather
                # than retrying into a block that only gets longer.
                if str(e.headers.get("X-RateLimit-Remaining") or "") == "0":
                    gh_trip_cooldown()
                    return e.code, {}
            if e.code in (403, 429) and attempt < 1:
                try:
                    wait = int(e.headers.get("Retry-After") or 0)   # honoured on secondary limits
                except Exception:
                    wait = 0
                time.sleep(min(max(wait, 15), 30))
                continue
            return e.code, {}
        except Exception:
            if attempt == 3:
                return -1, {}
            time.sleep(4)
    return -1, {}


def list_dir(repo: str, path: str, token: str) -> list[dict] | None:
    """Entries, [] if the directory is genuinely empty/absent, None if WE failed.

    Collapsing a throttled listing into [] made a transient 403 look like "no
    SKILL.md here" -- a permanent `entrypoint_absent` verdict with no retry
    budget, which is the same transient-becomes-permanent bug already fixed on
    the file-fetch path. 164 rows carry that verdict today.
    """
    st, body = api(f"https://api.github.com/repos/{repo}/contents/"
                   + urllib.parse.quote(path.strip("/")), token)
    if st == 200 and isinstance(body, list):
        return body
    return [] if st == 404 else None


def fetch_file_status(repo: str, path: str, token: str) -> tuple[int, bytes | None]:
    """(http_status, content). Keeping the status matters: a 404 means the source is
    genuinely gone (a permanent corpus fact worth a deterministic verdict), while a
    403/500/timeout means *we* failed and the row should stay retryable. v1 collapsed
    both into None and mislabelled drift as a fetch bug."""
    # Fast path: raw.githubusercontent. It does NOT consume the 5,000/hr core
    # quota and sustained 73 files/sec at 20-way concurrency with zero throttling
    # in measurement, against 3-way on the contents API (which we had throttled
    # to 3 precisely because bursting it trips GitHub's secondary limiter).
    #
    # Failures still fall through to the contents API rather than being trusted:
    # raw.gh answers 404 both for "file is gone" and "repo is private", and that
    # distinction is exactly what keeps a transient failure from being recorded
    # as a permanent corpus fact. The authoritative answer stays authenticated.
    rst, rbody = raw_url_fetch(repo, path)
    if rst == 200 and rbody is not None:
        return 200, rbody

    st, body = api(f"https://api.github.com/repos/{repo}/contents/"
                   + urllib.parse.quote(path), token)
    if st != 200 or not isinstance(body, dict):
        return st, None
    if body.get("encoding") != "base64":
        return st, None
    try:
        return st, base64.b64decode(body.get("content") or "")
    except Exception:
        return st, None


def fetch_file(repo: str, path: str, token: str) -> bytes | None:
    return fetch_file_status(repo, path, token)[1]


def fetch_entry(repo: str, path: str, token: str) -> tuple[int, dict | None]:
    """Raw contents-API body for a path, so callers can see `type`.

    GitHub exposes symlinks TWO different ways and v2.0 only handled one:
      (a) type "file" with the target's bytes already resolved — caught by the
          listed-size-vs-fetched-size mismatch heuristic;
      (b) type "symlink" with NO `content` field at all — invisible to (a), and
          v2.0 mislabelled it as a retryable HTTP-200 fetch failure.
    This returns the body so (b) can be detected directly, which is also a
    stronger signal than the heuristic.
    """
    st, body = api(f"https://api.github.com/repos/{repo}/contents/"
                   + urllib.parse.quote(path), token)
    return st, body if isinstance(body, dict) else None


def read_blob(repo: str, sha: str, token: str) -> bytes | None:
    st, body = api(f"https://api.github.com/repos/{repo}/git/blobs/{sha}", token)
    if st != 200 or not isinstance(body, dict) or body.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(body.get("content") or "")
    except Exception:
        return None


def detect_symlink(repo: str, entry_path: str, fetched_len: int, token: str,
                   listing: list | None = None) -> dict | None:
    """Is `entry_path` a git symlink (mode 120000)?

    GitHub does not expose the mode through the contents API, and fetching a symlink
    BY PATH silently RESOLVES it — you get the target's bytes with `type: "file"` and
    `target: null`, which is exactly what fooled run 1. The tell is the *directory
    listing*, which reports a symlink's size as the length of its target string. So a
    listed size that disagrees with the fetched size means symlink, and the entry's blob
    contains the target path.

    Returns {"target": <link text>} or None.
    """
    parent = str(PurePosixPath(entry_path).parent)
    name = PurePosixPath(entry_path).name
    for e in (listing if listing is not None else (list_dir(repo, parent, token) or [])):
        if e.get("name") != name:
            continue
        listed = e.get("size") or 0
        if listed == fetched_len or listed > 512:
            return None                      # sizes agree, or too big to be a link path
        blob = read_blob(repo, e.get("sha") or "", token)
        if blob is None:
            return None
        text = blob.decode("utf-8", "replace").strip()
        # a link target is a single short path with no newline
        if not text or "\n" in text or len(text) != listed:
            return None
        return {"target": text}
    return None


def resolve_symlink_target(entry_path: str, target: str) -> str | None:
    """Resolve a link target relative to the link's own directory, repo-internally.

    An absolute target (e.g. /Users/someone/... committed by mistake) can never be
    repo-internal, so it is rejected rather than silently reinterpreted as relative.
    """
    if target.startswith("/") or (len(target) > 1 and target[1] == ":"):
        return None
    base = PurePosixPath(entry_path).parent
    parts: list[str] = []
    for seg in (str(base / target)).split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                return None                  # escapes the repo root
            parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts) if parts else None


def build_tree(repo: str, skill_dir: str, token: str,
               top: list | None = None) -> list[dict]:
    """Skill dir listing plus one level of subdirectories (bounded)."""
    tree: list[dict] = []
    if top is None:
        top = list_dir(repo, skill_dir, token) or []
    subdirs = []
    for e in top:
        tree.append({"path": e.get("path"), "type": e.get("type"), "size": e.get("size") or 0,
                     "sha": e.get("sha")})
        if e.get("type") == "dir":
            subdirs.append(e.get("path"))
    for sd in subdirs[:MAX_SUBDIRS]:
        for e in (list_dir(repo, sd, token) or []):
            tree.append({"path": e.get("path"), "type": e.get("type"),
                         "size": e.get("size") or 0, "sha": e.get("sha")})
    return tree


def harvested_listing(repo: str, skill_dir: str) -> list[dict] | None:
    """Rebuild the bounded skill-directory listing from the local git tree.

    Tree harvest records every blob under a discovered skill directory with its
    authoritative git mode. Reusing that data avoids an authenticated contents
    API listing for the common case, while preserving symlink detection: mode
    ``120000`` remains visible locally. ``None`` means the repo has not been
    harvested yet, so callers must retain the normal GitHub API fallback.
    """
    prefix = skill_dir.strip("/") + "/"
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2)
        rows = con.execute(
            "select path,sha,mode,size from repo_trees where repo=? and path like ?",
            (repo, prefix + "%"),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None

    entries: dict[str, dict] = {}
    for full_path, sha, mode, size in rows:
        rel = full_path[len(prefix):]
        parts = rel.split("/")
        if len(parts) == 1:
            entries[full_path] = {
                "path": full_path,
                "name": parts[0],
                "type": "symlink" if mode == "120000" else "file",
                "size": int(size or 0),
                "sha": sha,
                "mode": mode,
            }
        elif len(parts) == 2:
            directory = prefix + parts[0]
            entries.setdefault(directory, {
                "path": directory, "name": parts[0], "type": "dir", "size": 0,
            })
            entries[full_path] = {
                "path": full_path,
                "name": parts[1],
                "type": "symlink" if mode == "120000" else "file",
                "size": int(size or 0),
                "sha": sha,
                "mode": mode,
            }
    return list(entries.values())


def resolve_entrypoint(skill: dict, token: str) -> dict:
    """Return {status, repo, entry_path, entry_hash, tree, detail}."""
    ref = skill["entrypoint_ref"]
    mode = ref.get("mode")
    if mode == "repo_path":
        repo, path = ref["repo"], ref["path"]
    elif mode == "repo_dir":
        repo, d = ref["repo"], ref["dir"]
        entries = list_dir(repo, d, token)
        if entries is None:
            return {"status": "fetch_failed", "repo": repo,
                    "detail": f"listing of {repo}:{d} failed (retryable)"}
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

    # ---- quota diet -----------------------------------------------------------
    # One dir listing serves THREE former quota calls: the entrypoint fetch, the
    # symlink probe's listing, and build_tree's top-level listing (the last two
    # were literally the same request twice -- measured 3.55 quota calls/skill,
    # which at size-800 batches quota-starved the fetch stage into a full-batch
    # failure). The listing is also the symlink arbiter: a listed size that is
    # implausible for a link target (>512) rules a symlink out, so those entry
    # bytes can come from raw.githubusercontent (off-quota). Small/ambiguous
    # entries keep the authoritative contents-API path, unchanged.
    skill_dir_early = str(PurePosixPath(path).parent)
    # Prefer the local harvested git tree. It carries exact file modes, so a
    # short regular file is safe to fetch from raw.githubusercontent without a
    # contents-API symlink probe. The API remains the fallback for unharvested
    # repos and raw-fetch failures.
    top_listing = harvested_listing(repo, skill_dir_early)
    if top_listing is None:
        top_listing = list_dir(repo, skill_dir_early, token) or None
    listed_entry = None
    if top_listing:
        for e in top_listing:
            if e.get("path") == path or e.get("name") == PurePosixPath(path).name:
                listed_entry = e
                break
    http_status, body = None, None
    content = None
    explicit_symlink = None
    if (listed_entry and listed_entry.get("type") == "file"
            and (listed_entry.get("mode") not in (None, "120000")
                 or (listed_entry.get("size") or 0) > 512)):
        rst, rbody = raw_url_fetch(repo, path)
        if rst == 200 and rbody is not None and len(rbody) == listed_entry.get("size"):
            http_status, content = 200, rbody
    if content is None:
        http_status, body = fetch_entry(repo, path, token)
    if body is not None:
        if body.get("type") == "symlink":
            # form (b): unresolved symlink, target lives in the blob
            blob = read_blob(repo, body.get("sha") or "", token)
            if blob is not None:
                explicit_symlink = {"target": blob.decode("utf-8", "replace").strip()}
        elif body.get("encoding") == "base64":
            try:
                content = base64.b64decode(body.get("content") or "")
            except Exception:
                content = None
    if content is None and explicit_symlink is None:
        if http_status == 200:
            return {"status": "entrypoint_unreadable",
                    "detail": (f"HTTP 200 but no usable content for {repo}:{path} "
                               f"(type={body.get('type') if body else None}, "
                               f"encoding={body.get('encoding') if body else None})"),
                    "repo": repo, "entry_path": path, "http_status": 200}
        if http_status == 404:
            # the file (or repo) no longer exists upstream: a real corpus fact
            return {"status": "source_deleted",
                    "detail": f"HTTP 404 for {repo}:{path} — no longer present upstream",
                    "repo": repo, "entry_path": path, "http_status": 404}
        return {"status": "fetch_failed",
                "detail": f"HTTP {http_status} for {repo}:{path} — retryable",
                "repo": repo, "entry_path": path, "http_status": http_status}
    if content is not None:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return {"status": "binary_entrypoint", "detail": "entrypoint is not UTF-8 text",
                    "repo": repo, "entry_path": path}
    else:
        text = ""          # unresolved symlink: the target supplies the real text below

    # --- v2 fix: a symlinked entrypoint must be judged as its TARGET -------------
    symlink = explicit_symlink or (
        detect_symlink(repo, path, len(content), token,
                       listing=top_listing if str(PurePosixPath(path).parent) == skill_dir_early
                       else None)
        if content is not None else None)
    provenance = None
    if symlink:
        resolved = resolve_symlink_target(path, symlink["target"])
        if not resolved:
            return {"status": "symlink_escapes_repo", "repo": repo, "entry_path": path,
                    "detail": f"symlink target {symlink['target']!r} leaves the repo root",
                    "entrypoint_symlink": {"from": path, "target": symlink["target"]}}
        target_content = fetch_file(repo, resolved, token)
        if target_content is None:
            return {"status": "symlink_target_missing", "repo": repo, "entry_path": path,
                    "detail": f"symlink {path} -> {resolved} could not be fetched",
                    "entrypoint_symlink": {"from": path, "target": symlink["target"],
                                           "resolved": resolved}}
        try:
            text = target_content.decode("utf-8")
        except UnicodeDecodeError:
            return {"status": "binary_entrypoint", "repo": repo, "entry_path": resolved,
                    "detail": "symlink target is not UTF-8 text"}
        content = target_content
        provenance = {"from": path, "target": symlink["target"], "resolved": resolved,
                      "link_bytes": len(symlink["target"])}
        path = resolved                     # judge the target, with the target's tree

    h = store_object(content)
    # Feed the blob index at fetch time: git sha -> content sha for the ENTRY
    # file, not just closure files. This is what lets verdict inheritance fire
    # for every future sighting of the same bytes without any fetch at all.
    if listed_entry and listed_entry.get("sha"):
        remember_blob(listed_entry.get("sha"), h)
    skill_dir = str(PurePosixPath(path).parent)
    # Reuse the listing fetched above unless a symlink moved us to a new dir.
    tree = build_tree(repo, skill_dir, token,
                      top=top_listing if skill_dir == skill_dir_early else None)
    out = {"status": "ok", "repo": repo, "entry_path": path, "entry_hash": h,
           "skill_dir": skill_dir, "tree": tree, "chars": len(text)}
    if provenance:
        out["entrypoint_symlink"] = provenance
    return out


def write_cache(cache: dict) -> None:
    """Atomic cache write. A plain write_text() is not atomic, so two runs racing
    on the same batch produced a file with two concatenated JSON documents
    ("Extra data: line 3846") and crashed the daemon on the next read."""
    tmp = FETCH_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    os.replace(tmp, FETCH_CACHE)


def read_cache() -> dict:
    """Tolerate a corrupt cache instead of crashing: it is a cache, so the worst
    case is re-fetching, never wrong data."""
    if not FETCH_CACHE.exists():
        return {}
    try:
        return json.loads(FETCH_CACHE.read_text())
    except Exception:
        try:
            FETCH_CACHE.rename(FETCH_CACHE.with_suffix(".json.corrupt"))
        except OSError:
            pass
        return {}


def stage_fetch(sample: dict, token: str, limit: int | None) -> dict:
    cache = read_cache()
    skills = sample["skills"][:limit] if limit else sample["skills"]
    RETRYABLE = {"fetch_failed"}
    gh_wait_cooldown("(fetch stage)")
    todo = [s for s in skills
            if s["id"] not in cache or cache[s["id"]].get("status") in RETRYABLE]
    # Canaries first. as_completed() below is bounded by FETCH_STAGE_DEADLINE and
    # simply drops whatever has not finished, so position in this list decides
    # what survives a truncated fetch. Canaries were scattered through it
    # (batch 163: positions 3..515 of 822), and when GitHub throttling stretched
    # the stage past its deadline -- ok=330, fetch_failed=471 -- the canaries went
    # with the tail, discarding an otherwise fine batch. They are 22 rows out of
    # ~820 and they decide whether the batch counts at all, so they get attempted
    # before everything else. Stable sort: all other ordering is preserved.
    todo.sort(key=lambda s: 0 if "canary" in (s.get("buckets") or []) else 1)
    print(f"fetch: {len(cache)} cached, {len(todo)} to fetch"
          f" ({sum(1 for s in todo if 'canary' in (s.get('buckets') or []))} canaries first)")
    done = 0
    ex = ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY)
    futs = {ex.submit(resolve_entrypoint, s, token): s for s in todo}
    try:
        for fut in as_completed(futs, timeout=FETCH_STAGE_DEADLINE):
            s = futs[fut]
            try:
                cache[s["id"]] = fut.result()
            except Exception as e:
                cache[s["id"]] = {"status": "fetch_failed", "detail": f"exception: {e}"}
            done += 1
            if done % 20 == 0:
                write_cache(cache)
                print(f"  fetched {done}/{len(todo)}", flush=True)
    except TimeoutError:
        stalled = [sk for f, sk in futs.items() if not f.done()]
        for sk in stalled:
            cache[sk["id"]] = {"status": "fetch_failed",
                               "detail": f"stage deadline {FETCH_STAGE_DEADLINE}s exceeded"}
        print(f"  fetch deadline hit: {len(stalled)} left retryable", flush=True)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    flush_blob_index()   # one locked merge instead of one per object
    write_cache(cache)
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


# A skill that cannot be fetched is retried, but not forever: unbounded retry
# would let a permanently-unreachable URL occupy a batch slot every round.
RETRY_LIMIT = int(os.environ.get("AUTOSKILL_FETCH_RETRY_LIMIT", "3"))


def prior_attempts(con, skill_ids: list[str]) -> dict[str, int]:
    """Attempts already recorded per skill id, from the stored verdicts."""
    out: dict[str, int] = {}
    if not skill_ids:
        return out
    q = ("select skill_id, output_json from enrichments where judge_role='deterministic'"
         " and skill_id in (%s)" % ",".join("?" * len(skill_ids)))
    try:
        for sid, oj in con.execute(q, skill_ids):
            try:
                out[sid] = int(json.loads(oj).get("_attempts") or 0)
            except Exception:
                out[sid] = 0
    except Exception:
        pass
    return out


def prefilter(skill: dict, fetched: dict, seen_hashes: dict,
              attempts: int = 0) -> dict | None:
    """Return a deterministic verdict dict, or None to send it to the judges."""
    st = fetched.get("status")
    if st != "ok":
        return {"is_real_skill": False, "confidence": 1.0, "vendor_convention": "unknown",
                "closure_paths": [], "summary": "", "triggers": [], "risk_flags": [],
                "reject_reason": {
                    "entrypoint_absent": "entrypoint absent",
                    "fetch_failed": "entrypoint could not be fetched from source",
                    "binary_entrypoint": "entrypoint is not text",
                    "symlink_escapes_repo": "entrypoint is a symlink pointing outside the repo",
                    "symlink_target_missing": "entrypoint is a symlink whose target is missing",
                    "source_deleted": "source file no longer exists upstream (HTTP 404)",
                    "entrypoint_unreadable": "source returned 200 but no usable content",
                }.get(st, st),
                "_rule": st, "_detail": fetched.get("detail"),
                # A retryable transport failure says nothing about the skill. Marking
                # it excluded_junk permanently discards real skills: batch 21 had 34
                # such rows, all HTTP 403 secondary-rate-limit, all returning 200 on
                # retry -- and four of them were CANARIES, which tripped a false STOP.
                # A transport failure is NEVER a verdict about the skill, no matter
                # how many times it failed. Retry exhaustion is a SCHEDULING fact
                # (batch building stops re-offering past RETRY_LIMIT via _attempts);
                # conflating it with judgement turned a transient 403 into
                # excluded_junk on a canary and tripped a false regression STOP.
                "_retryable": st == "fetch_failed",
                # Bounded retries. Releasing retryable failures back into the
                # pool is correct, but an endlessly-unreachable skill would then
                # occupy a batch slot every round forever. After RETRY_LIMIT
                # attempts it becomes terminal and stops being re-offered.
                "_attempts": attempts + 1}

    content = read_object(fetched["entry_hash"])
    text = content.decode("utf-8", "replace") if content else ""
    nh = norm_hash_of(text)

    # A canary's job is to be re-judged in EVERY batch, so it is a duplicate of
    # itself by construction. Letting the dedup rule fire on one excluded the
    # `matlab` canary as "exact-hash duplicate", which the canary check then read
    # as a quality regression and halted the whole fleet.
    if nh in seen_hashes and seen_hashes[nh] != skill["id"] \
            and "canary" not in (skill.get("buckets") or []):
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
    # v2.1: head+tail sampling. Head-only truncation buried the actual procedure in
    # files that open with a large boilerplate preamble (the `land-and-deploy` case:
    # a 73 KB skill whose deploy steps sat past a multi-KB shell preamble, so the
    # judge saw only preamble and rejected it as "truncated before the procedure").
    truncated = len(text) > ENTRY_CHAR_CAP
    if truncated:
        head_n = int(ENTRY_CHAR_CAP * 2 / 3)
        tail_n = ENTRY_CHAR_CAP - head_n
        elided = len(text) - ENTRY_CHAR_CAP
        shown = (text[:head_n]
                 + f"\n\n[... {elided} characters elided from the middle ...]\n\n"
                 + text[-tail_n:])
    else:
        shown = text
    tree = fetched.get("tree") or []
    tree_lines = "\n".join(
        f"  {e['path']}  ({e.get('type')}, {e.get('size', 0)} bytes)" for e in tree[:200]
    ) or "  (no sibling files listed)"
    block = (
        "<<<UNTRUSTED_SKILL_DATA>>>\n"
        f"REPO: {fetched.get('repo')}\n"
        f"ENTRYPOINT PATH: {fetched.get('entry_path')}\n"
        f"TRUNCATED: {'yes — head+tail sample of a %d-char file; the middle is elided and marked inline' % len(text) if truncated else 'no'}\n"
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
    clean["confidence"] = min(1.0, max(0.0, c))          # recorded only; gates nothing
    try:
        sp = float(out.get("specificity", 0.0))
    except Exception:
        sp = 0.0
        flags.append("specificity_unparseable")
    clean["specificity"] = min(1.0, max(0.0, sp))          # advisory only
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


def call_haiku_primary(prompt_text: str) -> dict:
    """Failover judge: headless `claude -p`, prompt on stdin, no tools.

    Same hostile-data posture as the secondary judge -- content never reaches a
    shell, never becomes argv, and the model gets no tools at all.
    """
    try:
        # --output-format json so the failover path reports REAL token usage.
        # It previously hard-coded tokens_in/out = 0, which made a failover run
        # invisible in the batch accounting -- exactly when we most need to know
        # what it costs, since it spends the Claude plan rather than codex.
        p = subprocess.run([CLAUDE_BIN, "-p", "--model", "haiku",
                            "--output-format", "json"],
                           input=prompt_text, capture_output=True,
                           text=True, timeout=LUNA_TIMEOUT)
        text, tin, tout = p.stdout or "", 0, 0
        try:
            env = json.loads(p.stdout or "{}")
            text = env.get("result") or ""
            u = env.get("usage") or {}
            tin = ((u.get("input_tokens", 0) or 0)
                   + (u.get("cache_creation_input_tokens", 0) or 0)
                   + (u.get("cache_read_input_tokens", 0) or 0))
            tout = u.get("output_tokens", 0) or 0
        except Exception:
            pass
        return {"text": text, "tokens_in": tin, "tokens_out": tout,
                "error": None if p.returncode == 0 else f"rc={p.returncode}",
                "returncode": p.returncode}
    except subprocess.TimeoutExpired:
        return {"text": "", "tokens_in": 0, "tokens_out": 0,
                "error": f"timeout after {LUNA_TIMEOUT}s", "returncode": -9}
    except Exception as e:  # noqa: BLE001 - never crash a batch on the judge
        return {"text": "", "tokens_in": 0, "tokens_out": 0,
                "error": f"{type(e).__name__}: {e}"[:200], "returncode": -1}


# ---- codex usage-limit blackout ------------------------------------------
# codex reports a hard reset time when the weekly cap is hit, e.g.
# "try again at Aug 11th, 2026 9:31 AM" -- measured 2026-08-07, four days out.
# Without recording it, every batch spends its whole primary stage rediscovering
# the same blackout (observed: made=427, ok=0, failed=427, 1818s per batch).
# Same shape as the GitHub cooldown: write the known-until time, and skip Luna
# entirely until it passes. Nothing is lost -- unjudged rows stay retryable and
# are re-offered once Luna answers again.
LUNA_BLACKOUT = Path("/srv/mobile-codex/autoskill-daemon/state/luna_blackout_until")
# Longest we will trust a reset time before re-probing. See note_luna_blackout.
BLACKOUT_MAX_SECONDS = 3600
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def luna_blackout_remaining() -> float:
    try:
        return max(0.0, float(LUNA_BLACKOUT.read_text(encoding="utf-8")) - time.time())
    except Exception:
        return 0.0


def clear_luna_blackout() -> None:
    """Remove a stale blackout marker after Luna has demonstrably answered."""
    try:
        LUNA_BLACKOUT.unlink(missing_ok=True)
    except OSError:
        pass


def note_luna_blackout(message: str) -> None:
    """Parse codex's own reset time out of a usage-limit message and record it.

    Falls back to a conservative 1h if the phrasing changes -- guessing LONGER
    than reality would idle the judge for no reason, so err short and re-probe.

    The stored deadline is CAPPED at BLACKOUT_MAX_SECONDS no matter what codex
    claims. `clear_luna_blackout()` only fires after a call succeeds, but the
    gate in stage_primary blocks every call while a blackout stands -- so a
    reset time that turns out to be too long can never self-heal, and the judge
    idles until someone notices by hand. Measured 2026-08-10: codex reported
    "try again at Aug 15th" and quota was actually back on Aug 10, which would
    have cost ~5 days of judging. Capping means the gate lapses hourly and one
    batched call re-probes; if Luna is still down that call fails, infra_failure()
    keeps it from fanning out to 32 retries, and this function re-arms. One
    wasted call per hour is the price of never idling on a stale deadline.
    """
    if "usage limit" not in (message or "").lower():
        return
    until = time.time() + 3600
    m = re.search(r"try again at ([A-Z][a-z]{2})\w*\s+(\d{1,2})\w*,?\s+(\d{4})\s+"
                  r"(\d{1,2}):(\d{2})\s*([AP]M)", message or "")
    if m:
        try:
            mon, day, yr, hh, mm, ap = m.groups()
            hh = int(hh) % 12 + (12 if ap.upper() == "PM" else 0)
            # `datetime` in this module is the CLASS (from datetime import
            # datetime), not the module -- datetime.datetime(...) raises here.
            until = datetime(int(yr), _MONTHS[mon], int(day), hh, int(mm)).timestamp()
        except Exception as exc:
            # Do not swallow silently: a parse failure means we fall back to 1h
            # and re-probe a judge we already know is down, which is exactly the
            # waste this function exists to remove.
            print(f"  luna blackout: could not parse reset time ({exc}); "
                  f"defaulting to 1h", flush=True)
    capped = min(until, time.time() + BLACKOUT_MAX_SECONDS)
    if capped < until:
        print(f"  luna blackout: codex claims reset at "
              f"{datetime.fromtimestamp(until):%Y-%m-%d %H:%M} "
              f"({(until - time.time())/3600:.1f}h); re-probing in "
              f"{BLACKOUT_MAX_SECONDS/3600:.0f}h instead", flush=True)
    try:
        LUNA_BLACKOUT.parent.mkdir(parents=True, exist_ok=True)
        LUNA_BLACKOUT.write_text(str(capped), encoding="utf-8")
    except Exception:
        pass


def call_luna(prompt_text: str) -> dict:
    """One sealed codex exec call. Empty temp cwd, scrubbed env, read-only sandbox."""
    if JUDGE_FAILOVER:
        return call_haiku_primary(prompt_text)
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
                note_luna_blackout(err)   # records codex's own reset time
        if p.returncode == 0 and not infra_failure(err or ""):
            clear_luna_blackout()
        text = outfile.read_text(encoding="utf-8") if outfile.exists() else ""
        return {"text": text, "tokens_in": tin, "tokens_out": tout,
                "error": err, "returncode": p.returncode}
    except subprocess.TimeoutExpired:
        return {"text": "", "tokens_in": 0, "tokens_out": 0,
                "error": f"timeout after {LUNA_TIMEOUT}s", "returncode": -9}
    finally:
        shutil.rmtree(jail, ignore_errors=True)


def call_luna_batch(items: list) -> tuple[dict | None, dict]:
    """Judge several skills in ONE sealed Luna call.

    Returns ({block_id: verdict}, usage). A batch that cannot be parsed, or that
    omits blocks, is NOT trusted for the missing entries — the caller falls back
    to single-skill calls for those, so batching can never silently lose verdicts.
    """
    # Failover uses the TUNED prompt. Measured blind vs Luna on identical bytes
    # (Sonnet grader, order-randomised): the stock prompt's `<= 50 words` upper
    # bound let Haiku drift to terse fragments and it lost 44.7% vs 50% parity.
    # Asking for 25-45 words, one complete sentence naming the concrete
    # technologies, moved it to 62.4% (n=250, +/-6.2pp) -- i.e. better than Luna.
    _bp = BATCHED_PROMPT
    if JUDGE_FAILOVER:
        _tuned = BENCH / "enrichment_prompt_v2_batched_haiku.md"
        if _tuned.exists():
            _bp = _tuned
    prompt = _bp.read_text(encoding="utf-8")
    parts = [prompt]
    for idx, (_s, _f, block, _nh, _tr) in enumerate(items, 1):
        inner = block.replace("<<<UNTRUSTED_SKILL_DATA>>>",
                              f"<<<UNTRUSTED_SKILL_DATA id={idx}>>>")
        inner = inner.replace("<<<END_UNTRUSTED_SKILL_DATA>>>",
                              f"<<<END_UNTRUSTED_SKILL_DATA id={idx}>>>")
        parts.append("\n" + inner)
    r = call_luna("\n".join(parts))
    parsed = parse_judge_json(r["text"])
    out: dict[int, dict] = {}
    if isinstance(parsed, dict) and isinstance(parsed.get("verdicts"), list):
        for v in parsed["verdicts"]:
            try:
                vid = int(v.get("id"))
            except Exception:
                continue
            if 1 <= vid <= len(items):
                out[vid] = v
    return out, r


def stage_primary(sample: dict, cache: dict, limit: int | None) -> dict:
    con = db()
    prompt = PRIMARY_PROMPT.read_text(encoding="utf-8")
    skills = sample["skills"][:limit] if limit else sample["skills"]

    seen_hashes: dict[str, str] = {}
    # How many times we have already failed to fetch each of these. Used to make
    # retries bounded -- see RETRY_LIMIT.
    prior = prior_attempts(con, [s["id"] for s in skills])
    pre_rows, judge_rows = [], []
    for s in skills:
        f = cache.get(s["id"], {"status": "fetch_failed", "detail": "not fetched"})
        v = prefilter(s, f, seen_hashes, attempts=prior.get(s["id"], 0))
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
        if already_judged(con, nh, "primary", primary_snapshot()):
            continue
        todo.append((s, f, block, nh, trunc))
    print(f"luna: {len(judge_rows) - len(todo)} already judged, {len(todo)} to call")
    # Canaries first so a budget trim can never drop a regression sentinel.
    def _is_canary(item):
        return "canary" in (item[0].get("buckets") or [])
    todo.sort(key=lambda it: 0 if _is_canary(it) else 1)

    # Luna is known-down until codex's own reset time. Every call in this window
    # fails identically, so making 400+ of them costs ~30min per batch and
    # returns nothing. Skip judging; fetch/inherit/package still run, and these
    # rows stay unjudged and retryable for when Luna answers again.
    _bo = luna_blackout_remaining()
    if _bo > 0 and not JUDGE_FAILOVER:
        print(f"  LUNA BLACKOUT: codex usage limit, {_bo/3600:.1f}h remaining "
              f"-- skipping {len(todo)} primary calls (0 spent)", flush=True)
        con.close()
        return {"made": 0, "retried": 0, "ok": 0, "malformed": 0, "failed": 0,
                "tokens_in": 0, "tokens_out": 0,
                "deferred_over_budget": len(todo), "luna_blackout": True}

    skill_budget = MAX_LUNA_CALLS * max(1, LUNA_BATCH_SIZE)
    trimmed = 0
    if len(todo) > skill_budget:
        trimmed = len(todo) - skill_budget
        n_can = sum(1 for it in todo[:skill_budget] if _is_canary(it))
        print(f"  CAP: {len(todo)} skills > budget {skill_budget} "
              f"({MAX_LUNA_CALLS} calls x {LUNA_BATCH_SIZE}); deferring {trimmed} "
              f"to the next run. Canaries kept: {n_can}")
        todo = todo[:skill_budget]
    calls_trimmed = trimmed

    calls = {"made": 0, "retried": 0, "ok": 0, "malformed": 0, "failed": 0,
             "tokens_in": 0, "tokens_out": 0, "deferred_over_budget": calls_trimmed}

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

    def work_batch(group):
        """One Luna call for the group; anything it fails to return falls back."""
        verdicts, r = call_luna_batch(group)
        results = []
        missing = []
        share_in = (r["tokens_in"] // max(1, len(group)))
        share_out = (r["tokens_out"] // max(1, len(group)))
        for idx, item in enumerate(group, 1):
            v = verdicts.get(idx)
            if v is None:
                missing.append(item)
                continue
            s, f, block, nh, trunc = item
            results.append((s, f, nh, trunc,
                            {"tokens_in": share_in, "tokens_out": share_out,
                             "error": None, "text": json.dumps(v)},
                            v, False))
        # The per-skill fallback exists for a MALFORMED or PARTIAL reply, where
        # asking again can genuinely recover the verdict. It cannot help when the
        # call failed for an infrastructure reason: a usage limit refuses the
        # retry exactly as it refused the batch, so one failed group of 32 became
        # 32 more doomed calls. Measured while codex was throttled -- made=427,
        # ok=0, failed=427, tokens_in=0, primary stage 1818s spent learning the
        # same refusal 427 times.
        # Fan out only when the batch call itself worked; otherwise record the
        # failure once per item. Those rows stay unjudged and retryable, so
        # nothing is lost -- they are re-offered once quota returns.
        if missing and infra_failure(r.get("error") or ""):
            for item in missing:
                _s, _f, _b, _nh, _tr = item
                results.append((_s, _f, _nh, _tr,
                                {"tokens_in": 0, "tokens_out": 0,
                                 "error": r.get("error"), "text": ""},
                                None, False))
            return results
        for item in missing:                      # never lose a verdict to batching
            results.append(work(item))
        return results

    groups = ([todo[i:i + LUNA_BATCH_SIZE] for i in range(0, len(todo), LUNA_BATCH_SIZE)]
              if LUNA_BATCH_SIZE > 1 else [[t] for t in todo])
    print(f"  batching: {len(todo)} skills -> {len(groups)} luna calls "
          f"(batch size {LUNA_BATCH_SIZE})")

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = [ex.submit(work_batch, g) for g in groups]
        for i, fut in enumerate(as_completed(futs), 1):
            for s, f, nh, trunc, r, parsed, retried in fut.result():
                calls["made"] += 1
                calls["retried"] += int(retried)
                calls["tokens_in"] += r["tokens_in"]
                calls["tokens_out"] += r["tokens_out"]
                tree_paths = file_paths(f)
                if parsed is None:
                    status = "failed(%s)" % (r.get("error") or "no_json")[:60] if r.get("error") \
                        else "malformed"
                    calls["failed" if r.get("error") else "malformed"] += 1
                    record_enrichment(con, nh, s["id"], s.get("url"), "primary", primary_snapshot(),
                                      {"raw_text": (r["text"] or "")[:2000],
                                       "error": r.get("error"), "truncated_input": trunc},
                                      r["tokens_in"], r["tokens_out"], status)
                else:
                    clean, flags = validate_output(parsed, tree_paths)
                    clean["truncated_input"] = trunc
                    calls["ok"] += 1
                    record_enrichment(con, nh, s["id"], s.get("url"), "primary", primary_snapshot(),
                                      clean, r["tokens_in"], r["tokens_out"], "ok")
                if i % 5 == 0 or i == len(futs):
                    print(f"  luna {i}/{len(futs)}  ok={calls['ok']} malformed={calls['malformed']}"
                          f" failed={calls['failed']} tok_in={calls['tokens_in']}", flush=True)

    # ---- build the secondary queue ------------------------------------------
    queue = []
    for s, f in judge_rows:
        block, nh, trunc = build_judge_input(s, f)
        prim = already_judged(con, nh, "primary", primary_snapshot())
        if not prim:
            continue
        o = prim["output"]
        # v2: confidence removed from logic — escalate on a primary reject
        # (or an unusable primary verdict) only.
        needs = (prim["status"] != "ok") or (not o.get("is_real_skill"))
        # Plus a deterministic ~3% audit stream of KEEPS. Every inclusion was
        # single-judge, so the corpus had no inter-judge agreement signal on
        # positives -- nothing to estimate label noise from and nothing to
        # train a cheap pre-filter against. Hash-keyed, so the same skill is
        # always in or out of the audit regardless of which batch drew it.
        if not needs and int(nh[:8], 16) % 33 == 0:
            needs = True
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
    # A primary blackout intentionally produces neither a secondary queue nor
    # results. Treat that matched absence as an empty secondary stage; any
    # one-sided absence remains an error so an actual lost escalation is never
    # silently accepted.
    if not SECONDARY_QUEUE.exists() and not results_path.exists():
        print("secondary ingest: no queue or results; nothing to ingest")
        return {"ok": 0, "malformed": 0, "unknown_hash": 0}
    con = db()
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    queue = json.loads(SECONDARY_QUEUE.read_text(encoding="utf-8"))
    by_hash = {i["norm_hash"]: i for i in queue["items"]}
    stats = {"ok": 0, "malformed": 0, "unavailable": 0, "unknown_hash": 0}
    for item in payload.get("results", []):
        nh = item.get("norm_hash")
        q = by_hash.get(nh)
        if not q:
            stats["unknown_hash"] += 1
            continue
        parsed = item.get("output") if isinstance(item.get("output"), dict) else \
            parse_judge_json(item.get("raw", ""))
        if parsed is None:
            raw = str(item.get("raw", ""))
            # The OAuth session can expire while Luna remains available. This
            # means Haiku never saw the skill, so it must not become a
            # malformed verdict that later blocks re-review. Leave the primary
            # rejection pending until independent review is available again.
            if any(marker in raw.casefold() for marker in SECONDARY_UNAVAILABLE_MARKERS):
                stats["unavailable"] += 1
                continue
            stats["malformed"] += 1
            record_enrichment(con, nh, q["skill_id"], q["url"], "secondary", HAIKU_SNAPSHOT,
                              {"raw_text": raw[:2000]}, 0, 0, "malformed")
            continue
        clean, _ = validate_output(parsed, set(q["tree_paths"]))
        record_enrichment(con, nh, q["skill_id"], q["url"], "secondary", HAIKU_SNAPSHOT,
                          clean, item.get("tokens_in", 0), item.get("tokens_out", 0), "ok")
        stats["ok"] += 1
    con.close()
    print("secondary ingest:", json.dumps(stats))
    return stats


# ==================================================================== combine

def reanchor_closure(paths: list[str], judged_dir: str | None, this_dir: str,
                     this_files: set[str]) -> tuple[list[str], list[str]]:
    """Re-anchor cached closure paths onto THIS row's tree.

    Verdicts are keyed by normalized entrypoint content, so the identical SKILL.md
    appearing at two different repo paths reuses one verdict. Its `closure_paths`
    are only meaningful relative to the location that produced them: fetching them
    verbatim against a different repo 404s every file. (Observed in batch 6: a
    citation-management skill reused a batch-3 verdict and lost all 13 closure
    files.) Dedup makes this the common case, not the edge case.

    Matching is by longest UNIQUE path suffix rather than by guessing a common
    prefix — prefix guessing breaks as soon as one cached path sits outside the
    skill directory, and the suffix is what actually identifies the file.
    """
    keep, unanchorable = [], []
    for p in paths:
        if p in this_files:
            keep.append(p)
            continue
        segs = p.split("/")
        matched = None
        for i in range(len(segs)):
            tail = "/".join(segs[i:])
            cands = [f for f in this_files if f == tail or f.endswith("/" + tail)]
            if len(cands) == 1:
                matched = cands[0]
                break
            if len(cands) > 1:
                break                     # ambiguous: refuse rather than guess
        if matched:
            keep.append(matched)
        else:
            unanchorable.append(p)
    return keep, unanchorable


# Directories that, by near-universal convention, hold a skill's supporting files.
DEP_DIR_RE = re.compile(
    r"/(scripts?|references?|assets?|templates?|examples?|data|bin|lib|prompts?|schemas?)/",
    re.IGNORECASE)


def dependency_paths(fetched: dict) -> list[str]:
    """Supporting files a skill plausibly needs, taken from its OWN tree.

    Measured on this corpus: of 397 kept skills that had dependency-shaped files
    sitting in their tree, 49 had Luna declare NO closure at all and 85 declared
    only some — 34% incomplete. Relying on the judge's `closure_paths` as the sole
    gate on what gets STORED loses real scripts and references (e.g.
    ensembl-database lost both references/api_endpoints.md and
    scripts/ensembl_query.py).

    So capture is now deterministic and the judge's list is an annotation of what
    is *essential*, not the decision about what is *kept*. Smallest files first so
    the per-skill cap spends itself on the many small references rather than one
    large asset.
    """
    entry = fetched.get("entry_path")
    out = []
    for e in (fetched.get("tree") or []):
        if e.get("type") != "file" or e.get("path") == entry:
            continue
        if DEP_DIR_RE.search("/" + (e.get("path") or "")):
            out.append((e.get("size") or 0, e["path"]))
    # textual first (rank 0), then binaries; smallest first inside each rank so the
    # cap buys the greatest number of useful files
    out.sort(key=lambda t: (0 if TEXTUAL_DEP_RE.search(t[1]) else 1, t[0], t[1]))
    return [p for _, p in out]


BLOB_INDEX = LIB / "blob_index.json"
_blob_cache: dict | None = None
# Buffered blob-index updates, flushed once per stage by flush_blob_index().
_blob_pending: dict[str, str] = {}


def blob_index() -> dict:
    """git blob sha -> content sha256 of bytes we have already stored.

    A git blob sha IS a hash of the exact bytes, so an entry here is proof we
    hold that content — no network call needed to find out. Measured on this
    corpus: 34% of closure fetches (690 of 2,042) were round-trips made purely
    to rediscover bytes already on disk, because the old code fetched first and
    checked the store afterwards.
    """
    global _blob_cache
    if _blob_cache is None:
        try:
            _blob_cache = json.loads(BLOB_INDEX.read_text(encoding="utf-8"))
        except Exception:
            _blob_cache = {}
    return _blob_cache


def remember_blob(blob_sha: str | None, content_sha: str) -> None:
    """Record git-blob-sha -> content-sha. BUFFERED; call flush_blob_index().

    This used to do the full merge-under-lock write on EVERY call: re-read and
    re-parse the whole blob_index.json, merge, and rewrite it. The index is now
    93,637 entries / 10.5 MB, so that is ~0.40s of pure JSON work per stored
    object, taken under a global exclusive flock.

    At ~819 new objects per batch that is ~327s -- and combine measured 360-397s,
    i.e. essentially the entire stage. It also explains why raising closure
    concurrency from 20 to 64 changed nothing: every fetch finished fast and then
    queued behind that one lock.

    Losing buffered entries to a crash costs a re-fetch, never correctness: the
    objects themselves are already written to the CAS, and this index only exists
    to avoid re-downloading bytes we can prove we hold.
    """
    if not blob_sha:
        return
    idx = blob_index()
    if idx.get(blob_sha) == content_sha:
        return
    idx[blob_sha] = content_sha        # visible to this process immediately
    _blob_pending[blob_sha] = content_sha


def flush_blob_index() -> int:
    """Merge buffered entries into the on-disk index. One locked write."""
    global _blob_cache
    if not _blob_pending:
        return 0
    import fcntl
    n = len(_blob_pending)
    lockp = BLOB_INDEX.with_suffix(".lock")
    try:
        with open(lockp, "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            # Re-read INSIDE the lock so this is a merge, not an overwrite --
            # several stages write this file concurrently and each used to clobber
            # the others' entries from its own stale cache.
            try:
                cur = json.loads(BLOB_INDEX.read_text(encoding="utf-8"))
            except Exception:
                cur = {}
            cur.update(_blob_pending)
            tmp = BLOB_INDEX.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cur), encoding="utf-8")
            os.replace(tmp, BLOB_INDEX)
            _blob_cache = cur
        _blob_pending.clear()
        return n
    except Exception:
        return 0                       # entries stay pending; next flush retries


def have_blob(blob_sha: str | None) -> bool:
    if not blob_sha:
        return False
    csha = blob_index().get(blob_sha)
    return bool(csha) and (LIB / "objects" / csha[:2] / csha[2:4] / csha).exists()


def fetch_closure(repo: str, paths: list[str], token: str,
                  sha_by_path: dict[str, str] | None = None,
                  known_tree: set[str] | None = None,
                  workers: int | None = None) -> dict:
    """Fetch a skill's supporting files, skipping anything we can prove we hold.

    Two independent savings, both quality-neutral:
      * `sha_by_path` maps tree path -> git blob sha. A git blob sha is a hash of
        the exact bytes, so an indexed blob is proof we already hold that content
        and needs NO network call. (Measured before this: 690 of 2,042 closure
        fetches — 34% — were round-trips made only to rediscover bytes on disk.)
      * the remaining fetches run concurrently. They are independent GitHub GETs
        with no shared state beyond the content-addressed store, which is atomic
        (write-temp + os.replace) and idempotent, so ordering cannot change what
        ends up stored — only how fast it gets there.
    """
    sha_by_path = sha_by_path or {}
    got, missing, present, skipped, throttled = [], [], [], [], []
    content_by_path: dict[str, str] = {}
    nonlocal_total = [0]
    todo = []
    for p in paths[:CLOSURE_MAX_FILES]:
        if have_blob(sha_by_path.get(p)):
            present.append(p)
            skipped.append(p)
            csha = blob_index().get(sha_by_path.get(p))
            if csha:
                content_by_path[p] = csha
        else:
            todo.append(p)

    def one(path: str):
        # Adjudicate without the REST API where we can. We listed this repo's
        # tree at fetch time, so a raw.gh miss on a path the tree contains is a
        # transport failure, not a deletion -- and burning a contents-API call
        # to learn that is exactly what exhausts the quota (measured: core hit
        # 0/5000 during this run, at which point only the off-quota path worked).
        rst, content = raw_url_fetch(repo, path)
        if rst == 200 and content is not None:
            return path, 200, content
        if known_tree is not None and path in known_tree:
            return path, 503, None      # known to exist -> retryable, no API call
        # Only the authenticated fallback is rate-limited. Holding the gate here
        # keeps the wide raw fan-out from queueing 60 threads into GitHub's
        # secondary limiter, where each 403 costs a 20/40/80s sleep inside api().
        with _API_GATE:
            st, content = fetch_file_status(repo, path, token)
        return path, st, content

    if todo:
        # `workers` lets a caller that is ALREADY fanning out across skills cap
        # the per-skill fan-out, so the two levels multiply to a bounded total
        # rather than 20 skills x 4 files = 80 concurrent GETs. GitHub's secondary
        # rate limit is a 403 on burst, and this pipeline has been bitten by it.
        workers = min(workers or CLOSURE_CONCURRENCY, len(todo))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for path, st, content in ex.map(one, todo):
                if content is None:
                    # 404 = genuinely gone. Anything else -- notably 403, GitHub's
                    # SECONDARY rate limit for concurrent requests -- means WE failed,
                    # not that the file is absent. Recording those as "missing" silently
                    # destroys closure data: it did exactly that to batch 11, marking 45
                    # files missing that all still exist.
                    if st == 404:
                        missing.append(path)
                    else:
                        throttled.append(f"{path} (HTTP {st})")
                    continue
                if len(content) > CLOSURE_MAX_BYTES:
                    missing.append(path + f" (>{CLOSURE_MAX_BYTES}B)")
                    continue
                nonlocal_total[0] += len(content)
                if nonlocal_total[0] > CLOSURE_MAX_TOTAL_BYTES:
                    missing.append(path + " (skill closure over total byte cap)")
                    continue
                h = hashlib.sha256(content).hexdigest()
                if (LIB / "objects" / h[:2] / h[2:4] / h).exists():
                    present.append(path)
                else:
                    store_object(content)
                    got.append(path)
                # Record path -> content hash EXPLICITLY. Previously this mapping
                # was only inferable via the tree's git blob sha, which just 58%
                # of cached trees carried; for the rest the closure bytes were on
                # disk but unaddressable by path, so packaging could not rebuild
                # the skill. The closure record is now self-sufficient.
                content_by_path[path] = h
                remember_blob(sha_by_path.get(path), h)
    return {"fetched": got, "already_present": present, "missing": missing,
            "served_from_index": skipped, "throttled": throttled,
            "content_by_path": content_by_path}


def _judged_dir_for(paths: list[str]) -> str | None:
    """Longest common directory of a cached closure-path set."""
    if not paths:
        return None
    parts = [PurePosixPath(p).parent.parts for p in paths]
    common: list[str] = []
    for seg in zip(*parts):
        if len(set(seg)) == 1:
            common.append(seg[0])
        else:
            break
    return "/".join(common) if common else None


def stage_combine(sample: dict, cache: dict, token: str, do_closure: bool) -> dict:
    con = db()
    rows = []
    canary_ids = {s["id"] for s in sample["skills"] if "canary" in s["buckets"]}
    # One pool shared across ALL skills, so the 20 configured slots are actually
    # used instead of 4-at-a-time inside each skill's own file list.
    # Phase timing: the closure fan-out fix did not move this stage as predicted
    # (480s vs an expected ~100s), so measure where it actually goes instead of
    # guessing a sixth time.
    import time as _ct
    CB_T = {}
    _cm = [_ct.time()]

    def cphase(name):
        now = _ct.time()
        CB_T[name] = CB_T.get(name, 0.0) + now - _cm[0]
        _cm[0] = now

    closure_pool = ThreadPoolExecutor(max_workers=CLOSURE_CONCURRENCY)
    closure_futs: list = []
    closure_stats = {"skills_with_closure": 0, "fetched": 0, "already_present": 0, "missing": 0}

    for s in sample["skills"]:
        f = cache.get(s["id"], {})
        if f.get("status") == "ok":
            c = read_object(f["entry_hash"])
            nh = norm_hash_of(c.decode("utf-8", "replace")) if c else None
        else:
            nh = "nofetch:" + hashlib.sha256(s["id"].encode()).hexdigest()[:56]
        det = already_judged(con, nh, "deterministic", "rule-engine-v1") if nh else None
        prim = already_judged(con, nh, "primary", primary_snapshot()) if nh else None
        sec = already_judged(con, nh, "secondary", HAIKU_SNAPSHOT) if nh else None

        rec = {"skill_id": s["id"], "name": s.get("name"), "url": s.get("url"),
               "source": s.get("source"), "buckets": s["buckets"], "norm_hash": nh,
               "is_canary": s["id"] in canary_ids,
               "fetch_status": f.get("status"), "repo": f.get("repo"),
               "entry_path": f.get("entry_path"),
               "entrypoint_symlink": f.get("entrypoint_symlink")}

        # A canary is hand-verified ground truth: it IS a real skill, and it is
        # deliberately re-judged every batch. So a deterministic rule excluding
        # one is always a bug in the rule, never a fact about the skill -- and
        # fixing the rule is not enough, because verdicts persist. The dedup rule
        # fired on `matlab` once, and the resulting `is_real_skill: false` row
        # outlived the fix, was re-served from cache, then spread to six more
        # canaries via blob-sha inheritance and halted the fleet a second time.
        # Refuse the exclusion and surface it loudly instead of silently
        # dropping the sentinel that exists to catch exactly this.
        if det and rec["is_canary"] and not det["output"].get("_retryable"):
            reason = det["output"].get("reject_reason") or det["output"].get("_rule")
            print(f"  BUG: deterministic rule tried to exclude canary "
                  f"{rec['name']!r} ({reason}) -- refusing, rule is wrong",
                  file=sys.stderr, flush=True)
            det = None

        if det and det["output"].get("_retryable"):
            rec["label"] = "pending"
            rec["decided_by"] = "deterministic"
            rec["reason"] = f"retryable fetch failure -- {det['output'].get('_detail')}"
            rec["rule"] = det["output"].get("_rule")
        elif det:
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
            # Quarantine means "this skill looks suspicious enough to withhold".
            # A primary that never answered says nothing about the skill, so it
            # must not produce that label. Luna hit a usage limit and 56 skills
            # were quarantined for it -- withheld from the corpus because our
            # quota ran out, which is a fact about us, not about them.
            # Content-level failures (the judge answered, unparseably, about
            # THIS text) still warrant a hold; transport and quota failures are
            # simply "not known yet" and go back in the queue.
            if sec and sec["status"] == "ok" and not infra_failure(prim["status"]):
                rec["label"] = "quarantine"
                rec["reason"] = f"primary {prim['status']}; secondary responded"
                rec["secondary"] = {k: sec["output"].get(k)
                                    for k in ("is_real_skill", "confidence", "reject_reason")}
        else:
            po = prim["output"]
            p_keep = bool(po.get("is_real_skill"))
            rec["primary"] = {"is_real_skill": p_keep,
                              "confidence": po.get("confidence"),
                              "specificity": po.get("specificity"),
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
            if not p_keep:
                if sec is None:
                    rec["label"] = "pending"
                    rec["decided_by"] = "primary"
                    rec["reason"] = "secondary review required but not returned"
                elif sec["status"] != "ok":
                    rec["label"] = "quarantine"
                    rec["decided_by"] = concur_label()
                    rec["reason"] = f"secondary {sec['status']}"
                else:
                    so = sec["output"]
                    s_keep = bool(so.get("is_real_skill"))
                    rec["secondary"] = {"is_real_skill": s_keep,
                                        "confidence": so.get("confidence"),
                                        "specificity": so.get("specificity"),
                                        "reject_reason": so.get("reject_reason"),
                                        "summary": so.get("summary"),
                                        "closure_paths": so.get("closure_paths"),
                                        "model_self_report": so.get("model_self_report")}
                    rec["decided_by"] = concur_label()
                    if not p_keep and not s_keep:
                        rec["label"] = "excluded_junk"
                        rec["reason"] = po.get("reject_reason") or so.get("reject_reason")
                    elif p_keep and s_keep:
                        rec["label"] = "included"
                        rec["reason"] = "primary rejected, secondary kept -- see quarantine rule"
                    else:
                        rec["label"] = "quarantine"
                        rec["reason"] = ("judges disagree: primary "
                                         f"{'keep' if p_keep else 'reject'}, "
                                         f"secondary {'keep' if s_keep else 'reject'}")
            else:
                rec["label"] = "included"
                rec["decided_by"] = "primary"
                rec["reason"] = "primary keeps"

        # ---- post-verdict gates ------------------------------------------------
        # Audit 2026-08-03 found both of these annotated but never enforced:
        # 340 hard-flagged rows were INCLUDED corpus-wide against 3 excluded
        # (9 prompt_injection, 43 malware_indicators, 22 destructive_commands),
        # and 102 rows under eval-fixture paths were served as real skills --
        # 39 of them cases from a red-team dataset, some carrying the judge's
        # own injection flag. A flag the corpus ignores is not a safety control.
        if rec["label"] == "included":
            flags = set((rec.get("primary") or {}).get("risk_flags") or [])
            hard = flags & HARD_RISK_FLAGS
            if rec.get("is_canary") and (hard or FIXTURE_PATH_RE.search(
                    "/" + (f.get("entry_path") or ""))):
                # Ground truth wins over any heuristic. Surfaced so the gate can
                # be recalibrated rather than silently eroding the canary set.
                rec["gate_would_have_fired"] = sorted(hard) or ["fixture_path"]
                print(f"  GATE MISCALIBRATION: canary {rec.get('name')!r} would have "
                      f"been gated by {rec['gate_would_have_fired']}", flush=True)
            elif hard:
                rec["label"] = "quarantine"
                rec["reason"] = ("hard risk flag(s): " + ", ".join(sorted(hard))
                                 + " -- held for review, not served")
                rec["decided_by"] = "risk_gate"
            elif FIXTURE_PATH_RE.search("/" + (f.get("entry_path") or "")):
                rec["label"] = "excluded_junk"
                rec["reason"] = ("entrypoint lives under an eval/test fixture path "
                                 "-- a dataset case, not a deployed skill")
                rec["decided_by"] = "fixture_gate"

        # bounded closure fetch for anything kept
        if do_closure and rec["label"] in ("included", "quarantine") and f.get("repo"):
            raw_paths = (rec.get("primary") or {}).get("closure_paths") or []
            paths, unanchorable = reanchor_closure(
                raw_paths, None, f.get("skill_dir") or "", file_paths(f))
            # judge-declared paths keep priority inside the cap; deterministic
            # dependency files then top it up so nothing obvious is silently lost
            judge_set = set(paths)
            merged = list(paths)
            for extra in dependency_paths(f):
                if extra not in judge_set:
                    merged.append(extra)
            # Rank the MERGED list text-first, not just the deterministic tail.
            # Otherwise a judge that names a few binaries burns cap slots ahead of
            # reference docs: manimgl-best-practices held 100 files yet still lost
            # 5 of its 87 example scripts that way.
            sizes = {e["path"]: (e.get("size") or 0) for e in (f.get("tree") or [])}
            merged.sort(key=lambda q: (0 if TEXTUAL_DEP_RE.search(q) else 1,
                                       0 if q in judge_set else 1,
                                       sizes.get(q, 0), q))
            rec["closure_declared_by_judge"] = len(paths)
            rec["closure_added_deterministically"] = len(merged) - len(paths)
            paths = merged
            if unanchorable:
                rec["closure_unanchorable"] = unanchorable
                closure_stats["unanchorable"] = closure_stats.get("unanchorable", 0) + len(unanchorable)
            if paths:
                sha_by_path = {e["path"]: e.get("sha") for e in (f.get("tree") or [])
                               if e.get("path")}
                # Submit instead of calling. fetch_closure parallelises WITHIN one
                # skill's file list -- min(CLOSURE_CONCURRENCY, len(todo)) -- and
                # skills carry only ~4 closure files, so it was using 4 of the 20
                # configured slots (20% utilisation) and running ~332 skills as
                # ~332 SEQUENTIAL GitHub round-trips at ~1.48s each. That is the
                # whole ~490s of this stage. Fanning out ACROSS skills keeps every
                # fetch identical and simply stops them queueing behind each other.
                closure_futs.append((rec, closure_pool.submit(
                    fetch_closure, f["repo"], paths, token, sha_by_path,
                    {e["path"] for e in (f.get("tree") or [])
                     if e.get("type") == "file"},
                    1)))
        rows.append(rec)

    # Resolve the closure fetches. Stats are accumulated here, in submission
    # order, so the recorded totals are identical to the serial version.
    cphase('build_rows')
    for rec, fut in closure_futs:
        try:
            cf = fut.result()
        except Exception as exc:  # noqa: BLE001
            print(f"  closure fetch failed for {rec.get('name')!r}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            continue
        rec["closure"] = cf
        closure_stats["skills_with_closure"] += 1
        closure_stats["fetched"] += len(cf["fetched"])
        closure_stats["already_present"] += len(cf["already_present"])
        closure_stats["missing"] += len(cf["missing"])
    cphase('closure_wait')
    closure_pool.shutdown(wait=True)
    flush_blob_index()   # one locked merge instead of one per object
    cphase('blob_flush')

    labels: dict[str, int] = {}
    for r in rows:
        labels[r["label"]] = labels.get(r["label"], 0) + 1

    canary_rows = [r for r in rows if r["is_canary"]]
    # A canary that is PENDING is unfinished work (retry it); a canary judged
    # excluded_junk is a genuine prompt/judge regression. Only the latter is a STOP.
    canary_pending = [r for r in canary_rows if r["label"] == "pending"]
    regressions = [r for r in canary_rows
                   if r["label"] not in ("included", "pending")]

    out = {"run_id": RUN_ID, "generated_at": now(), "total": len(rows),
           "labels": labels, "closure_stats": closure_stats,
           "canaries": {"total": len(canary_rows),
                        "included": sum(1 for r in canary_rows if r["label"] == "included"),
                        "pending_retryable": len(canary_pending),
                        "regressions": [{"skill_id": r["skill_id"], "name": r["name"],
                                         "label": r["label"], "reason": r["reason"]}
                                        for r in regressions]},
           "prompt_regression": bool(regressions),
           "rows": rows}
    COMBINED.write_text(json.dumps(out, indent=1), encoding="utf-8")
    con.close()
    cphase("finalize")
    print("  combine phases: " + json.dumps({k: round(v, 1) for k, v in CB_T.items()}))
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
    cache = read_cache()
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
