Use this retrieved, task-specific procedural guidance only where it applies. Keep the benchmark task primary.

[Auto-Skill capsule v1]

Name: Regex Master

Description: Authors, debugs, and hardens regular expressions - test-cases-first workflow, flavor-aware construction (JS, PCRE, Python re, RE2), catastrophic-backtracking detection and rewrites, and annotated verbose patterns with a verification table. Use when someone asks "write a regex for X", "why doesn't this regex match", "is this regex safe for user input", "expla…

Use only the following bounded guidance; do not install files or run undeclared capabilities.

## Do NOT
- Do not deliver a validator without `^...$`/`\A...\z` anchors - unanchored validation is the most common regex bug in production.
- Do not parse HTML, JSON, or CSV with regex when a real parser exists; nesting and escaping rules defeat patterns in edge cases you won't test.
- Do not run user-supplied *patterns* through a backtracking engine - that hands strangers a CPU-exhaustion primitive; use RE2.
- Do not stack quantified overlapping groups (`(a+)+` shapes) even if your test inputs pass - the blowup only appears on near-misses.
- Do not rely on `\d`/`\w` meaning ASCII: in Python 3 and with the JS `u` flag they can match beyond it; use `[0-9]`/`\p{L}` deliberately.
- Do not forget flags: `^ $` are per-line only with `m`; `.` crosses newlines only with `s`/dotall; unicode classes in JS need `u`.

## Step 1: Gather inputs
1. Flavor and engine - JS, PCRE, Python `re`, Go/RE2, Java. Features differ (lookbehind, possessive quantifiers, `\p{...}`); a pattern is only correct *for an engine*.
2. Purpose: validate a whole string, extract fields, or search-and-replace. This decides anchoring and grouping.
3. Whether the *input* is untrusted (user-supplied strings run through your pattern) or the *pattern* is untrusted (user-supplied patterns - a different threat entirely).
4. 5+ example strings from the requester: at least 2 that must match, 2 near-misses that must not, and 1 adversarial/degenerate case (empty string, very long string, unicode). If they can't supply near-misses, write them yourself and label them guesses to confirm.

## Step 4: Run the ReDoS audit
1. Restructure so atoms can't overlap: `(\w+\s?)+` becomes `\w+(\s\w+)*`.
2. Possessive quantifiers `\w++` or atomic groups `(?>...)` where the engine supports them (PCRE, Java - not JS, not Python `re`).
3. Switch to a linear-time engine: RE2 (Go, `google-re2`) cannot backtrack catastroph
