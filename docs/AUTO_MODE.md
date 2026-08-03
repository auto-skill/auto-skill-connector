# Auto Mode and Routing Modes

## CLI on-demand mode

Nothing intercepts prompts. A user or agent explicitly calls `search`,
`route`, `preview`, or `route_prompt`.

## Connected MCP proactive routing

Adding the MCP connector deliberately makes its routing tools available.
Its server instructions ask capable client models to call the read-only
`route_task` once near the start of substantial work, without waiting for
the user to ask for a skill. The model must send a concise task summary and
omit secrets, personal data, pasted content, and irrelevant conversation
history. This improves recall but is not a prompt interceptor, so a client
may still ignore the instruction.

The routing tools advertise read-only, non-destructive, idempotent
semantics. Auto-Skill itself does not require per-task approval and never
executes or installs anything, although a client may display tool
activity. Any later shell, network, filesystem, deployment, or other side
effect still follows the client's normal permission and approval rules.

## Auto Mode (opt-in adapter)

Auto Mode is a client integration, not a property of MCP: a client-side
hook locally preflights the raw prompt and calls the route service
directly, instead of depending on a model choosing to call an MCP tool.
Enabling it allows the adapter to inspect eligible prompts, call the route
service, and add verified task-specific context for the current turn. Tiny
acknowledgements, commands, meta/status prompts, and pasted context are
filtered locally without a server call.

Auto Mode never means automatic persistent installation. It can use
high-confidence, content-hash-verified, risk-0, static instructions in the
current task without a modal; ambiguous matches stay as candidate hints,
and unverifiable content is not injected. Normal client permissions still
govern every tool, script, network call, and side effect mentioned by those
instructions.

Supported clients today:

- **Claude Code** — `auto-skill enable-hook` registers `hooks/skill_suggest.py`
  as a `UserPromptSubmit` hook in `~/.claude/settings.json`.
- **Codex CLI** — `auto-skill enable-hook --target codex` registers the same
  script as a `UserPromptSubmit` hook in `~/.codex/config.toml`. Codex's hook
  contract accepts the identical stdin shape (`{"prompt": "..."}`) and treats
  plain stdout as injected context, so the hook script itself is unmodified;
  only the registration format (TOML block vs. JSON entry) differs. Run
  `auto-skill disable-hook --target codex` to remove it.

Not supported yet, blocked on the vendor, not on this project:

- **Cursor CLI** — its `beforeSubmitPrompt` hook (added to the CLI in July
  2026) can only allow/block a prompt (`{"continue": bool, "user_message":
  str}`); it has no field for injecting context or skill content. There is
  an open Cursor forum request asking for this.
- **GitHub Copilot CLI** — its `userPromptSubmitted` hook is explicitly
  fire-and-forget; stdout is never read. Only `sessionStart`,
  `subagentStart`, `postToolUse`, and `notification` support
  `additionalContext`, and none of those fire per-prompt with the task text
  available.

Both clients still benefit from the MCP proactive task-summary instruction
above; they just can't get deterministic raw-prompt interception until
their hook APIs add a context-injection field.

## Enabling and disabling

Enable an adapter only after reading `SECURITY.md`. Auto Mode is shipped
for Claude Code and Codex CLI; Cursor and Copilot stay MCP-only for inject.

```bash
auto-skill enable-hook                 # Claude Code (~/.claude/settings.json)
auto-skill enable-hook --target codex  # Codex CLI (~/.codex/config.toml)
```

Each command shows a privacy notice and asks for confirmation. Once
enabled, eligible prompt text is sent to the configured route service so it
can select task-specific skill context. Neither the hosted backend nor the
local routing log retains raw prompt text or snippets. Operational events
retain only privacy-safe metadata such as prompt length, route tier,
selected skill, latency, client/version, and outcome. Hosted routing still
requires login.

Disable at any time:

```bash
auto-skill disable-hook
auto-skill disable-hook --target codex
```

Cursor and Copilot have no raw-prompt inject hook yet (vendor limit). Their
connected MCP models can still proactively call `route_task`. When a client
cannot isolate a large skill, Auto-Skill falls back to a deterministic
capsule rather than silently injecting the full document.

## Diagnostics and analytics (both off by default)

Local diagnostics are off by default. Set `AUTOSKILL_DIAGNOSTICS=1` only
when you need a metadata-only routing log for troubleshooting. The current
hook removes legacy prompt fields from an older routing log on its next
run; if you have not upgraded the hook, delete
`~/.claude/auto-skill-routing.jsonl`.

Anonymous usage analytics are also off by default. If you explicitly opt in
by setting `AUTOSKILL_ANONYMOUS_ANALYTICS=1`, the local adapter creates one
random installation UUID in `~/.autoskill/installation.json` and sends it
with routes. The backend stores only a one-way hash of that UUID, never the
UUID, prompt, IP address, or a machine fingerprint. Remove the file (or
unset the variable) to reset/stop anonymous tracking. Account
authentication always takes priority over the anonymous ID.
