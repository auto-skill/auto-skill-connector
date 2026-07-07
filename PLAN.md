# Launch Plan

Goal: a stranger can read the README, run one install command, get the always-on
skill-routing hook working in Claude Code, and see a *correct* skill routed on
their first real task. Public GitHub launch in a few weeks.

Decisions this plan is built on (interview, 2026-07-06):

- The Claude Code hook is THE product. CLI, MCP server, and hosted connector are
  delivery mechanisms for the same router.
- Routing policy is threshold + suggestion hybrid: high confidence → inject full
  SKILL.md; medium → inject a one-line hint; low → inject nothing. Prefer
  silence over a wrong injection.
- Two repos with clear roles: `auto-skill-connector` (public product: CLI, MCP
  server, hook — one canonical copy of each) and `auto-skill` (indexing
  pipeline + hosted service). `tool\skill-scraper` is an obsolete copy and gets
  deleted.
- Launch finish line: public GitHub launch (demo GIF, honest README, stranger
  test, HN/Reddit/X post).

## System state as verified on 2026-07-06

What actually works (better than the public repo suggests):

- The corpus is real: 202,367 rows migrated from Supabase into
  `local_skills.db` (now the single source of truth), 308,821 content-cached
  entries in `skills_library/`, 33k skills harvested from skillsmp.
- The public endpoints WORK. `skills.avalahome.com` and `mcp.avalahome.com`
  are Cloudflare-tunneled to this PC (cloudflared running, public DNS →
  Cloudflare edge). Verified: `/find-semantic` and the MCP `initialize`
  handshake both return correctly from the public side.
- A similarity floor already exists (`MIN_SIMILARITY = 0.87`, calibrated
  2026-07-06) plus a recommend-vs-clarify gap heuristic (`RECOMMEND_GAP`) and
  optional Ollama reranking in `recommender.py`.
- An eval harness exists (`eval_search.py`): 30 positive cases across three
  engines (keyword/vector/hybrid) plus 7 negative gate cases.
- Warm `/find-semantic` latency is ~0.18s. The ~4.4s seen earlier is cold
  start (embedding model load).

Real problems found:

1. **Stub skills pass the gate.** Live incident during this review: the hook
   routed a prompt to "autoplan", whose entire SKILL.md body is one absolute
   file path from a stranger's machine. It cleared the similarity floor and
   the HTML check and got injected as instructions. Content-quality validation
   is missing at both index time and injection time.
2. **Public unauthenticated `install_skill` writes files to this PC.**
   `mcp.avalahome.com/mcp` exposes `install_skill` to anyone with the URL; it
   writes SKILL.md files onto the machine running the server. The README's
   "add your own access control" warning is not enough once the URL is
   published.
3. **The Supabase edge function serves a frozen corpus.** Storage moved local
   on 2026-07-05; Supabase no longer receives updates, but it's still the
   fallback in the connector repo's chain and the primary in one hook variant.
   Fresh results and stale results now differ silently by backend.
4. **Split-horizon DNS breaks the public hostname on your own LAN.** Local
   resolver returns 192.168.68.99 for the avalahome names and the LAN box has
   no TLS cert for them, so the shipped default `https://skills.avalahome.com`
   fails on this machine (the `AUTOSKILL_URL=http://localhost:8000` override
   in your settings is what saves the local hook).
5. **Drift everywhere: two different hooks, two different MCP servers.**
   `auto-skill/hooks/skill_suggest.py` (suggestion-line, Supabase-first) vs
   `auto-skill-connector/hooks/skill_suggest.py` (full-content injection,
   self-hosted-first); `auto-skill/mcp_server.py` (2 tools, local content) vs
   `auto-skill-connector/mcp_server.py` (4 tools incl. route_prompt/route_task).
   The public tunnel serves the pipeline repo's older 2-tool server.
6. Cold start: the first routed prompt after a service restart exceeds the
   hook's 3.0s timeout and silently falls back (or drops to a stale backend).
7. Truncated Supabase anon key in `auto_skill_core.py:16` and the connector
   hook (accepted today, but wrong).

## Phase 1 — Routing correctness (core product)

1. Content-quality gate for injection: reject SKILL.md payloads that are too
   short (< ~200 chars), lack frontmatter or any imperative body, or consist
   mostly of paths/links. Apply at injection time in the hook AND at index
   time in the pipeline (the `skills_library` cache already has the content —
   flag stubs so they never rank).
2. Implement the hybrid tier in the hook using the `similarity` score the
   backend already returns: `>= T_full` → full SKILL.md injection;
   `>= MIN_SIMILARITY` but `< T_full` → one-line hint ("a skill exists for
   this: <name> — <url>"); below → nothing. Calibrate `T_full` with the eval
   set.
3. Extend `eval_search.py`: grow negatives to ~25 (include real derailments
   like the autoplan prompt), add a stub-content check, and report the
   full/hint/none decision per case, not just retrieval hits.
4. Cold-start fix: pre-load the embedding model at service startup (warm-up
   embed of one string) so the first hook call doesn't blow the 3s budget.
5. Single canonical hook: the connector repo's full-injection hook becomes the
   only one; delete `auto-skill/hooks/`. Point settings at it.

## Phase 2 — Public backend (revised: the tunnel IS the backend)

1. Keep the Cloudflare-tunneled service as the primary public backend — it
   already works and serves the fresh corpus. Harden it as infra: run
   cloudflared + scraper as Windows services that survive reboots, add a
   `/healthz` endpoint, and a simple uptime check.
2. Decide Supabase's role and make it honest: either (a) resume syncing the
   local corpus to Supabase so the edge fallback is fresh, or (b) drop the
   Supabase fallback from shipped code and fail to "no route" when the tunnel
   is unreachable. Recommendation: (b) for launch — one truthful backend beats
   two inconsistent ones; the hook fails open anyway.
3. Fix the truncated anon key wherever Supabase remains in use.
4. LAN ergonomics: keep `AUTOSKILL_URL` override documented; note the
   split-horizon DNS situation in the README's self-hosting section.

## Phase 3 — Lock down the public connector (before ANY launch publicity)

1. Remove or auth-gate `install_skill` on the public transport: bearer token
   required for any tool that writes to disk, or serve a read-only toolset
   (recommend/route only) publicly and keep install local-stdio-only.
   Recommendation: read-only public toolset.
2. Serve the connector repo's 4-tool MCP server through the tunnel instead of
   the pipeline repo's older 2-tool one (after step 1).
3. Rate-limit the public endpoints (Cloudflare rules are enough).

## Phase 4 — Install UX

1. `auto-skill enable-hook` / `disable-hook`: writes/removes the
   UserPromptSubmit entry in `~/.claude/settings.json`, prints the privacy
   note (prompt snippets go to the search backend) and requires confirmation.
   Cross-platform paths.
2. Extend `auto-skill doctor`: backend reachability + warm latency, hook
   registered?, hook script path valid?, python resolvable?
3. README quickstart: install → `auto-skill enable-hook` → `auto-skill doctor`
   → type a task in Claude Code.

## Phase 5 — Trust and safety

1. Document the risk scanner honestly: pattern-based heuristics over fetched
   content, filters `risk_score >= 3`, not a malware guarantee. Align
   SECURITY.md.
2. Keep source URL + risk score in every injected header (already done).
3. Injection provenance log: append routed skill name/url/score/tier to a
   local log so a derailed session can be diagnosed.

## Phase 6 — Consolidation and quality

1. Delete `C:\Users\Neel\tool\skill-scraper`.
2. One canonical MCP server and one canonical hook (connector repo); pipeline
   repo keeps scraper/embeddings/risk/eval/serving only.
3. Add a parity test asserting the stdlib-only hook's gating logic matches
   `auto_skill_core.py`.
4. CI: GitHub Actions running the connector test suite on push.

## Phase 7 — Launch polish

1. Demo GIF: a real Claude Code session where a prompt routes to a skill and
   the output is visibly better.
2. Three worked examples in the README (e.g. xlsx report, PDF work, frontend).
3. Honest README pass: every claim reproducible by a stranger; the hosted
   connector section stays only if Phase 3 is done.
4. Stranger test: someone installs from scratch on a clean machine following
   only the README. Fix everything they hit.
5. Ship: HN / r/ClaudeAI / X post.
