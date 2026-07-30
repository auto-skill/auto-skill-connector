Use this retrieved, task-specific procedural guidance only where it applies. Keep the benchmark task primary.

[Auto-Skill capsule v1]

Name: git-secret-remediation

Description: Remove committed secrets from Git history safely and verify remediations across local and remote repositories

Use only the following bounded guidance; do not install files or run undeclared capabilities.

## When to use
- Secrets, tokens, keys, or passwords were committed to Git
- `.env` or config files with sensitive values entered repository history
- A history rewrite is required before a repository is shared broadly
- Rotated credentials still need forensic cleanup from Git objects

## Purpose
Remove accidentally committed secrets from Git history in a controlled, auditable way. This skill covers rewriting history, handling cross-platform text replacement pitfalls, and validating that sensitive values are removed from both local and remote history.

## Output format
1. **Incident scope summary** - what leaked and where
2. **Replacement rule file** - reproducible redaction mappings
3. **Rewrite command log** - exact commands run and refs touched
4. **Verification report** - proof that leaked values are absent post-rewrite
5. **Recovery actions** - collaborator reset instructions and prevention controls

## Example usage
> A secret was committed and merged into multiple branches. Help me run a safe `git filter-repo` remediation workflow on Windows, including a BOM-free replace-text file, verification commands, and collaborator recovery steps.

---

_Source: This skill is sourced from the [Matrix Skills](https://github.com/POWR-DATA/mtx-skills) library. Learn more at the [AI Agent Skills Library](https://powrdata.com.au/ai-agent-skills)._

## Guiding principles
- **Rotate first, rewrite second.** Credential rotation reduces immediate risk while history cleanup is prepared.
- **Use deterministic replacement rules.** Build an explicit replacement map and verify exact matching before rewrite.
- **Windows encoding can silently break replacement maps.** In Windows PowerShell, `Set-Content -Encoding UTF8` writes a BOM that can prevent `git filter-repo --replace-text` matches.
- **Write replacement files as BOM-free UTF-8.** Use .NET UTF-8 encoding with BOM disabled to avoid hidden prefix bytes.
- **Treat force-push as a coordinated change event.** Notify collaborators and require fresh clones or hard resets after rewrite.
