# Auto-Skill Backend

FastAPI, SQLite, embedding search, deterministic routing, content provenance,
and deployment safety for Auto-Skill.

The backend supports a trusted, compatible Agent Skill lifecycle across Claude
Code, Codex, and Cursor. Ingestion breadth is an input, not the product moat.
The public contract is quality-gated discovery and routing with verifiable
content integrity, privacy-safe events, and predictable failure behavior.

This remains deliberately small single-host infrastructure. A database or
hosting rewrite is not a launch requirement.

## Run Locally

```powershell
python -m pip install -r requirements.txt
python scraper.py
```

Launch-facing endpoints:

- `GET /healthz`: process liveness.
- `GET /readyz`: SQLite, active embedded rows, vector cache, and scraper
  readiness.
- `POST /find-semantic {"q":"...","limit":8}`: body-only ranked discovery
  (auth required, same as `/route`).
- `POST /route {"task":"..."}`: deterministic full/hint/none route contract
  (auth required; Free quota applies).
- `GET /content/{content_hash}`: content-addressed `SKILL.md` snapshot bytes.
- `POST /route-feedback`: privacy-safe route outcome feedback.
- `GET /route-metrics`: host-local aggregate latency/token operational metrics.

The public API does not retain raw prompts or prompt snippets. Hosted
`POST /route` requires login (`401` with signup URL when missing). Free
accounts get **100 authenticated routes/month**
(`AUTOSKILL_FREE_ROUTES_PER_MONTH`). CLI tokens use a **30-day sliding** TTL
(refreshes on use; idle 30 days → re-login). Authentication is also used for
private skills, favorites, caller-specific history, and hosted MCP identity.

## Product and Internal Surfaces

`POST /route` is the launch routing contract. It is deterministic and does not
depend on a chat model.

The Ollama `/chat` recommender and `backend/index.html` are local development
experiments. The HTML page is an internal scraper/admin panel, not the public
product. Do not expose or position either as the launch experience.

`recommend_skill` is a deprecated compatibility preview surface. New clients
should use `/route` through `route_task` so ambiguity and no-route outcomes are
represented honestly.

The Supabase-shaped `/rest/v1/*` compatibility surface is loopback-only. It
lets the scraper, worker, and recommender share the SQLite store; public clients
must use the launch-facing endpoints above.

Raw prompt `/route-skip` analytics are not part of the product contract.
Client preflight skips happen locally without a network call.

## Client Compatibility

The backend serves portable `SKILL.md` content rather than a Claude-only
format. Client code owns the native install destination:

- Claude Code: `~/.claude/skills`
- Codex: `~/.agents/skills`
- Cursor: `~/.agents/skills` (Cursor also supports `~/.cursor/skills`)

MCP cannot invisibly intercept every client prompt, but its server instructions
ask capable clients to call the read-only `route_task` once for substantial
work using a privacy-minimized summary. Raw-prompt Auto Mode still requires a
client-specific, explicitly enabled adapter. Shipped adapters: Claude Code and
Codex CLI (`enable-hook` / `enable-hook --target codex`). Cursor and Copilot
remain MCP-only for inject until their vendors add a context-injection field.

## Quality and Integrity Gate

Fresh ingest writes deterministic quality metadata:

- `quality_status`: `pending`, `active`, `metadata_only`, `rejected`, or
  `duplicate`.
- `quality_reasons`: machine-readable gate reasons.
- `quality_score`: 0-100 structural/content score.
- `content_hash`: normalized SHA-256 used for integrity checks and dedupe.
- `platforms` and `category`: inexpensive routing tags.
- source URL and canonical identity metadata for provenance.

Only `active` rows with valid `SKILL.md` frontmatter (`name` and
`description`) are eligible for embedding. `metadata_only` rows may appear as
hints. Unscanned rows remain `pending` rather than becoming routable through a
schema default.

A canonical content hash proves that the local indexed snapshot matches its
normalized digest; full route responses also carry a raw served-byte digest.
It does not query upstream HEAD, verify the publisher, guarantee safety, or
evaluate instruction correctness. Publisher verification and signed/pinned
releases are P1.

Backfill existing rows before public routing. Stop API/worker writes, or use
the VPS maintenance sequence in `RUNBOOK.md`, because the script reclassifies
the corpus and invalidates vectors for changed or quarantined content:

```powershell
python backfill_quality.py
python reindex.py
python launch_check.py --base-url http://127.0.0.1:8000
```

For a quick status read before the heavier launch gate:

```powershell
python launch_status.py
```

## Routing Policy

Runtime routing uses local embeddings for retrieval, then deterministic
reranking with lexical overlap, quality score, platform mismatch, and a capped
popularity prior. Platform-specific skills are capped to `hint` unless the
task names that platform.

The tiers are:

- `full`: for public skills, high-confidence, `risk_score=0`, valid content
  whose local snapshot matches the canonical hash and raw served digest, with
  no detected tool, script, network-command, dependency-install, dangerous-
  shell, or no-confirmation capability.
  Eligible for current-task use only.
- `hint`: ambiguous, unverified, risky, incomplete, platform-mismatched, or
  capability-bearing content. Includes up to three content-free candidates.
- `none`: no eligible candidate cleared the routing floor.

Risk and no-confirmation gates catch some dangerous patterns, but cannot
understand every script, network request, dependency, permission, secret, or
side effect mentioned by a skill. Client permissions remain the enforcement
boundary; these gates are not a malware guarantee.

Ollama chat selection is disabled by default. `ENABLE_OLLAMA_CHAT=1` is for
local experiments only; production `/route` does not depend on an LLM.

## Privacy-safe Route Events

The router does not store raw task text or prompt snippets. Operational events
may include:

- caller/user id when authentication is present;
- an optional one-way hash of a client-generated anonymous installation ID
  when the client explicitly opts into `AUTOSKILL_ANONYMOUS_ANALYTICS=1`;
- the caller's IP address (proxy-reported client IP, validated as a real
  address before storage);
- query length;
- client and client version;
- selected skill, tier, result count, and outcome;
- route, retrieval, rerank, and content latency;
- estimated input, candidate, raw-content, capsule, injected, and response tokens; and
- context delivery (public routes use `capsule` or `hint`) and capsule size; and
- router configuration version.

Skipped client prompts are not sent to a separate analytics endpoint. Legacy
feedback notes are ignored and never retained; feedback is enum-only and works
for anonymous route IDs with rate limiting.

`GET /route-metrics` is host-local and aggregates operational budgets. It is
not a consumer analytics dashboard. The public read-only guard intentionally
does not expose it.

Anonymous installation hashes and IP addresses expire from route events after
`AUTOSKILL_ANONYMOUS_ID_RETENTION_DAYS` (90 days by default). The hash
identifies a persisted installation for coarse retention/conversion metrics,
not a person; authenticated `user_id` remains the authoritative identity.
The restrained admin API does not return route IPs, raw prompts, private skill
content, bearer tokens, or token hashes.

Default warning budgets are 750 ms total latency, 500 ms skill-find time,
1000 injected tokens, and 3500 response tokens.

`/route` retains the legacy `guard_mode`, `supports_isolation`, and
`max_inline_chars` fields for wire compatibility, but public scraped content is
never returned raw or as an isolation payload. `max_capsule_chars` is bounded
at 2400. Routing never installs a skill or writes one to disk.

Public search matches are hint-only by default. Full delivery requires the
exact safe-capsule SHA-256 digest to appear in the comma-separated
`AUTOSKILL_VALIDATED_CAPSULE_DIGESTS` allowlist after replicated task outcome
validation. The capsule digest is task- and provenance-bound and never
authorizes delivery of the underlying raw `SKILL.md`. The
`AUTOSKILL_EXPERIMENTAL_UNVALIDATED_PUBLIC_FULL=1` bypass is for controlled A/B
runs only, not production routing.

New GitHub and marketplace ingestion is package-first. GitHub tree URLs are
resolved to a commit and a complete subtree; if any file cannot be captured or
the immutable package safety limits are exceeded, the candidate is rejected
instead of silently shortened. The optional official
`skills.sh` curated API capture stores its complete file snapshot and registry
hash when `SKILLS_SH_OIDC_TOKEN` is configured. At route time, the same token
enables the live skills.sh data gate: Auto-Skill sends the original and
compiled multi-word queries to `/api/v1/skills/search`, hydrates only a bounded
shortlist through the detail endpoint, and fetches audit metadata before
ranking. The live rows carry the stable skills.sh ID, snapshot hash, audit
state, and a session-scoped `npx skills use <source> --skill <name> --agent codex`
activation plan. The plan is bounded metadata for a client adapter; the
backend never executes `npx` or writes to the caller's filesystem. Without a
token, the same route calls the public skills.sh discovery lane and keeps those
metadata-only rows hint-only. If skills.sh is unavailable, public routing
abstains by default; `AUTOSKILL_ALLOW_LOCAL_RETRIEVAL_FALLBACK=1` is an explicit
offline/outage experiment switch, never a production default. SkillsMP discovery remains
capped at 100 unique URLs per run by default. Unpinned GitHub content is
retained for triage with `pending_package` status and is not embedded. Package
bytes, paths, hashes, licenses, roles, references, and source aliases remain
separate from the single entrypoint-first 1,500-character retrieval record, so
package integrity does not imply all-file embedding. GitHub, SkillsMP, and
curated-list content without a complete package is discovery metadata only and
cannot become an active instruction route.

Collector delta exports use format v2 when immutable package tables are
available. The archive carries complete package manifests and content-addressed
source objects, not just the curated entrypoint body. Audit a collector or
production database before applying an import:

```bash
python backend/audit_package_integrity.py \
  --db backend/data/local_skills.db \
  --package-root backend/skills_library/packages
```

An `ok: true` result means every active GitHub/SkillsMP/curated-list row has a
complete manifest and every referenced source object hashes correctly.

For existing catalogs, `backend/deploy/hydrate-source-packages.sh` provides a
bounded, resumable repair pass. It captures the full public codeload tree,
quarantines malformed/oversized/ambiguous sources, and writes immutable
manifests/objects around a stopped-reader SQLite window. Set `LIMIT`,
`BATCHES`, `RETRY_FAILED`, and (for a long online run) `STOP_SERVICES=0`; a
partial pass intentionally leaves the audit
non-green until more batches finish. After hydration,
`backend/deploy/backfill-source-embeddings.sh` re-embeds active rows with the
API stopped and restarts it to refresh its vector cache.
The operator defaults to `LIMIT=0` and `BATCH_SIZE=8`; use bounded `LIMIT`
windows on small droplets so the ONNX runtime cannot spike memory. Set
`BATCHES` to repeat bounded windows while keeping readers stopped once.
On the current 1.5 GiB API droplet, keep `STOP_SERVICES=1`: the serving ONNX
runtime and a concurrent hydrator exceed the host memory budget.

Conversation follow-ups carry the stable skills.sh ID, not an arbitrary source
URL. Live mode rehydrates that ID through the skills.sh catalog (using the
short-lived search cache for public metadata-only rows); it never reloads a
legacy local-corpus row. A raw GitHub URL without a skills.sh ID is rejected by
the live follow-up path rather than treated as verified discovery.

When no OIDC token is available, the live gate uses the public website search
endpoint only. Those rows are explicitly `metadata_only` hints with unknown
audit state and no content hash; they cannot become capsules or full routes.

Authenticated detail and audit requests are bounded and retried on transient
`429`/`502`/`503`/`504` responses. Tune `SKILLS_SH_MAX_CONCURRENT_REQUESTS`,
`SKILLS_SH_MIN_REQUEST_INTERVAL_SECONDS`, `SKILLS_SH_MAX_RETRIES`, and
`SKILLS_SH_RETRY_BASE_SECONDS` for the provider's effective quota. A persistent
rate limit still causes the route or benchmark to abstain; it must not be
treated as a successful safety check.

Runtime routing is local-first once a skills.sh record has been hydrated. The
record is persisted in the SQLite mirror at `SKILLS_SH_MIRROR_DB_PATH`. Detail
and audit freshness are controlled by `SKILLS_SH_MIRROR_STALE_SECONDS`, but
mirror rows and immutable source blobs do not expire from discovery; stale rows
remain searchable as hints until refreshed. Identical cold queries are
single-flighted, so concurrent users share one upstream search/detail/audit
sequence. Mount the mirror on durable shared storage (or replace it with the
deployment's shared catalog store) before scaling across API replicas; the
normal user path should not call skills.sh per request.

Warm the mirror after deployment with an authenticated, quota-bounded sync.
The recommended flow refreshes the short-lived token into the mounted file,
then runs the sync:

```bash
VERCEL_PROJECT=autoskill-indexer \
bash backend/deploy/refresh-and-sync-skills-sh.sh
```

The job also includes the official curated set by default, skips fresh mirror
rows on reruns, filters detected duplicates, and hydrates only a small batch
of detail/audit records at a time. Set `VERCEL_PROJECT` to the dedicated
Auto-Skill Vercel project before refreshing. The token is mounted read-only and
read on each request, so rotation does not require an API restart. Inline
`SKILLS_SH_OIDC_TOKEN` remains available only for a one-shot emergency run.
The combined command is safe to run from cron or a systemd timer; it refreshes
the token, walks every leaderboard page in metadata-only mode, hydrates the
bounded top slice, and verifies that the shared mirror is non-empty.

To build a complete local discovery index, use the resumable metadata-only mode:

```bash
python backend/sync_skills_sh_mirror.py --all-listings --view all-time \
  --per-page 500 --hydrate-top 100
```

This walks the API's `pagination.hasMore` pages and stores every non-duplicate
listing as a searchable `metadata_only` hint. Only the bounded `--hydrate-top`
slice fetches package files and audit records, so the full catalog does not
turn into millions of detail/audit requests or trusted content.

For a resumable full hydration pass across every discovered listing, use:

```bash
python backend/sync_skills_sh_mirror.py --all-listings \
  --views all-time,trending,hot --per-page 500 \
  --hydrate-all --retry-failed --batch-size 8 --delay-seconds 0.25
```

The deployed operator wrapper for this explicit full pass is
`backend/deploy/reingest-skills-sh-full.sh`; the ordinary refresh job remains
bounded by design.

Hydrated source blobs are retained separately from compact retrieval metadata,
with per-skill ingestion attempts and reason-level counters. Permanent failures
are skipped on later runs unless `--retry-failed` is supplied; transient detail,
audit, and stale-mirror failures remain eligible for retry.

### Seeding a production mirror

Do not copy the mirror into `skills_library` or a client skill directory. The
shared runtime database is the host file
`/opt/auto-skill-connector/backend/data/local_skills.db`, mounted into the
containers as `/data/local_skills.db`. The normal code deploy deliberately
excludes `backend/data/` so it cannot overwrite live state.

Before transfer, rebuild a verified snapshot. The compactor preserves every
canonical mirror row, source package, manifest, hash, provenance field, and
ingestion attempt while rebuilding the FTS index once:

```bash
python backend/compact_skills_sh_mirror.py \
  backend/.skills_sh_mirror.db \
  backend/.skills_sh_mirror.compacted.db
```

The explicit seed operator compresses that snapshot, verifies SHA-256 on both
ends, validates SQLite integrity, stops readers and Litestream, checkpoints
old WAL state, copies the existing application database into a staging file,
replaces only the `skills_sh_*` mirror tables, rebuilds FTS, validates the
merged database, atomically swaps the complete staged application database,
keeps a rollback backup, and only then starts the readers again. It refuses to
run if the existing application database is missing or its mirror schema does
not match; it never replaces the application database with the mirror snapshot
wholesale.

```bash
DEPLOY_HOST=... \
DEPLOY_USER=... \
DEPLOY_SSH_KEY_PATH=... \
bash backend/deploy/seed-skills-sh-mirror.sh \
  backend/.skills_sh_mirror.compacted.db
```

It requires real SSH access and `sudo` on the droplet; it never runs as part
of an ordinary Git archive deploy.

## Evals

Track retrieval quality, route latency, and token churn across changes:

```powershell
python eval_search.py --json-out eval-results/latest.json
python eval_compare.py eval-results/before.json eval-results/latest.json
python bench/evidence_eval.py query --output eval-results/query-heldout.json
python bench/evidence_eval.py outcomes eval-results/agent-outcomes.jsonl `
  --output eval-results/outcome-gate.json
python bench/evidence_eval.py parity data/local_skills.db `
  --output eval-results/v6-parity.json
# Live skills.sh gate (requires SKILLS_SH_OIDC_TOKEN or VERCEL_OIDC_TOKEN)
python -m bench.skills_sh_live_eval --cases bench/skills_sh_cases.jsonl `
  --output eval-results/skills-sh-live.json
```

The outcome input requires at least two replicates for each of `no-skill`,
`raw-skill`, and `distilled-capsule`. Tasks with a perfect no-skill control are
excluded. The report includes paired wins/losses, bootstrap confidence
intervals, cost, latency, tokens, safety failures, and strategy displacement.
The parity gate must pass before a hosted result can be attributed to this
router/corpus version.

`bench.skills_sh_live_eval` reports original-query versus structured-query
hit@1/hit@5, paired wins/losses, paired bootstrap confidence intervals, audit
coverage, and latency. The checked-in cases use stable skills.sh IDs as
held-out labels; expand the set before using it as a production gate. Without
the documented OIDC token it runs in `public_search_only` mode and keeps all
results metadata-only; if even the public search endpoint is unavailable, it
emits `status: skipped`.

The fresh fair 10-case review split in
`bench/skills_sh_live_verified_fair_20260729.json` measured original versus
original-plus-compiled retrieval at 80%→90% hit@1 and 90%→100% hit@5, with one
paired win, zero losses, and paired 95% bootstrap intervals of [0, 30]
percentage points. This is a retrieval signal, not a task-success or
production-safety claim: the run used public search only, so all 50 inspected
candidate rows had unknown audit state. The next gate is authenticated
detail/audit coverage plus replicated no-skill/raw-skill/distilled-capsule
outcome evaluation.

The first live public-search pilot is retained at
`bench/skills_sh_live_20260729_public.json`. It recorded zero hits for the
three preselected IDs; those IDs were not human-verified against the current
catalog, so the artifact is a diagnostic, not a success/failure claim.

Route benchmark cases live in `evals/routes.jsonl`. Add false positives,
direct hits, ambiguous matches, and conversation/meta negatives so behavior
changes appear in snapshots without editing Python.

For a production-safe latency baseline, capture a repeated route profile and
compare it after a deployment. The profile retains only public case IDs, route
tier, selected skill name, and numeric metrics; it does not write prompts,
skill content, warnings, or credentials.

```powershell
python route_profile.py --json-out eval-results/route-before.json
python route_profile_compare.py --fail-on-regression `
  eval-results/route-before.json eval-results/route-after.json
```

`route_profile.py` uses the local CLI credential by default, or an
`AUTOSKILL_EVAL_TOKEN` supplied only for the process. Use a dedicated test
account for repeated production measurements so the benchmark does not consume
customer quota or mix with customer analytics.

## OAuth Redirect Configuration

Dashboard OAuth redirects are allowlisted. Set
`AUTO_SKILL_DASHBOARD_ORIGINS` to a comma-separated list of trusted origins,
for example:

```text
https://autoskill.dev,https://www.autoskill.dev
```

Local development origins are allowed by default when the variable is not set.
Hosted routing requires an account (Free: 100 routes/month). Private skills,
favorites, and caller-specific history also use authentication. Pro/Team
billing surfaces exist but stay quiet in launch messaging until P1 trust.

## Readiness and Vector Cache

`/readyz` and `/route-metrics` include vector-index statistics so latency can
be correlated with active corpus size, valid embeddings, and matrix-cache
state. `/readyz` also includes scraper-running, stale-run, last-success, and
recent-run summaries.

The in-process vector matrix cache is invalidated immediately on skill writes,
updates, and deletes, so freshly embedded rows do not wait for the TTL before
becoming searchable.

## Deploy Skeleton

`deploy/docker-compose.yml` is a small VPS-oriented stack:

- `api`: public/read-oriented FastAPI service.
- `admin-local`: founder admin UI/API bound to droplet `127.0.0.1:8002` on a
  dedicated network excluded from `cloudflared`; reach it only through SSH
  port forwarding.
- `mcp`: authenticated streamable-HTTP connector with no skill/filesystem
  write tool, routed to the API
  over the compose network.
- `worker`: an off-by-default `collector` profile for a single trusted
  discovery/embedding pass; it is not a production service.
- `cloudflared`: tunnel to the API and MCP connector.
- `litestream`: SQLite WAL replication to Cloudflare R2.
- `library-backup`: daily R2 tarballs for `skills_library/` until all content is
  stored in SQLite.
- `db-inspector`: an on-demand, read-only Datasette profile bound to
  `127.0.0.1:8001`; reach it only through SSH port forwarding.

The public API hard-disables `/admin`. The separate `admin-local` service
serves it only on `ADMIN_HOST=127.0.0.1`; SSH key possession and an Auto-Skill
bearer for an exact `ADMIN_EMAILS` address are required. The UI keeps that
token in `sessionStorage` only. Admin mutations are limited to expiring
complimentary access grants/revocations with an append-only audit trail; paid
plan and seat changes remain Stripe-owned.

The hosted MCP connector must never register a filesystem install tool. MCP
OAuth establishes caller identity; it does not make server-host writes a safe
way to install onto a caller's machine.

For launch, prefer this single-host SQLite/Litestream/R2 path over a managed
database rewrite. `RUNBOOK.md` documents the evidence required before moving
to Turso, Postgres/pgvector, or another vector backend.

Before starting the stack:

```powershell
python deploy\compose_preflight.py
```

The manual GitHub deploy workflow preserves its full transcript as a
30-day `deploy-diagnostics-<run>-<attempt>` artifact, including failed
deployments. It retries only the pre-mutation SSH connectivity check and
archive upload with bounded backoff. Once remote extraction begins it remains
fail-fast and uses the normal rollback path rather than replaying a partial
production operation.

For an intentionally restricted direct SSH operator, run the same script with
`AUTOSKILL_DEPLOY_REMOTE_SUDO=1`. It executes remote deployment commands via
non-interactive `sudo` but never displays or copies the root-owned production
environment file.

The deploy verifies ownership of the live database and skills-library paths;
it does not recursively change ownership of Litestream or backup history.

The current app still stores content under `skills_library/`, so that directory
needs its own backup until content migration is complete.

Before moving the emergency Windows/laptop service, create a backup:

```powershell
.\deploy\backup-local.ps1 -PackContentBlobs
```

Add `-UploadR2` when R2 credentials are configured. Timestamped local backups
are pruned after 14 days by default. Use
`.\deploy\install-windows-tasks.ps1` to register service loops and daily local
backup in Task Scheduler.

Backup manifests include file sizes and SHA-256 hashes. Verify before restore
or after download:

```powershell
python deploy\verify_backup.py data\backups\20260708T200000Z
```

Build gzip content-addressed blobs with:

```powershell
python pack_content_blobs.py
```

See `RUNBOOK.md` for deployment, rollback, recovery, and backup operations.
