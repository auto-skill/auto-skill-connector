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
- `POST /find-semantic {"q":"...","limit":8}`: body-only ranked discovery.
- `POST /route {"task":"..."}`: deterministic full/hint/none route contract.
- `GET /content/{content_hash}`: content-addressed `SKILL.md` snapshot bytes.
- `POST /route-feedback`: privacy-safe route outcome feedback.
- `GET /route-metrics`: host-local aggregate latency/token operational metrics.

The public API does not retain raw prompts or prompt snippets. It does not need
an individual account solely to search or route; authentication remains useful
for private skills, favorites, caller-specific history, and hosted MCP identity.

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
client-specific, explicitly enabled adapter; only the Claude Code adapter ships
in the launch scope.

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
- estimated input, candidate, content, injected, and response tokens; and
- context delivery (`full`, `capsule`, or `isolation`) and capsule size; and
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

`/route` accepts `guard_mode` (`hybrid` by default), `supports_isolation`,
`max_inline_chars` (default 4000), and `max_capsule_chars` (default 2400).
Large verified static skills are returned as deterministic capsules when the
client cannot provide an isolated context. Routing never installs a skill or
writes one to disk. Set `AUTOSKILL_CONTEXT_GUARD=0` only for a temporary
compatibility rollback.

## Evals

Track retrieval quality, route latency, and token churn across changes:

```powershell
python eval_search.py --json-out eval-results/latest.json
python eval_compare.py eval-results/before.json eval-results/latest.json
```

Route benchmark cases live in `evals/routes.jsonl`. Add false positives,
direct hits, ambiguous matches, and conversation/meta negatives so behavior
changes appear in snapshots without editing Python.

## OAuth Redirect Configuration

Dashboard OAuth redirects are allowlisted. Set
`AUTO_SKILL_DASHBOARD_ORIGINS` to a comma-separated list of trusted origins,
for example:

```text
https://autoskill.dev,https://www.autoskill.dev
```

Local development origins are allowed by default when the variable is not set.
Accounts are for private/caller-specific features, not mandatory public
discovery tracking.

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
- `worker`: scraper and embedding loop, writing through the local REST surface.
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
