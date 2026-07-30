Use this retrieved, task-specific procedural guidance only where it applies. Keep the benchmark task primary.

[Auto-Skill capsule v1]

Name: recover-sqlite-database

Description: Check Neotoma SQLite integrity and run .recover when the DB is corrupt (malformed disk image, btree errors). Use when MCP/API fails with SQLite corruption.

Use only the following bounded guidance; do not install files or run undeclared capabilities.

## When to use
Neotoma errors such as **`database disk image is malformed`**, failed **`PRAGMA integrity_check`**, or **`btreeInitPage` / SQLITE_CORRUPT** on the local SQLite file (`neotoma.db` or `neotoma.prod.db` under `NEOTOMA_DATA_DIR`).

## Write recovered copy (does not replace live DB)
Output is a sibling file like `neotoma.prod.recovered-<timestamp>.db`. Verify it reports **`PRAGMA integrity_check: ok`**, then **manually** archive the live `.db`/`-wal`/`-shm` and **`cp`** the recovered file to the live name.

npx neotoma storage recover-db --env production --recover
```

## Do not
- Run **`--recover`** while Neotoma still holds the database open.
- Auto-delete the corrupt file until a good recovered copy is verified and backed up.

## After swap
- Inspect **`lost_and_found`** in the recovered file if present; re-ingest important rows via **`store_structured`** using normal schemas.
- Prefer **`NEOTOMA_DATA_DIR` outside iCloud-synced folders** to reduce recurrence.

## Preconditions
- **`sqlite3`** CLI on `PATH` (macOS usually has it).
- **Stop Neotoma** (MCP stdio processes, `neotoma api`, anything using the DB) before **`--recover`** or before swapping files.

## Script (primary)
From the Neotoma install location:

## Check only (exit 0 = ok, 2 = corrupt)
npx neotoma storage recover-db
npx neotoma storage recover-db --env production
