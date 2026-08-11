# Production 502 window — diagnosis and reversible ops fix

Run `corpusv1-20260802`. Host `157.245.168.172`, user `waliul`, key `~/.ssh/id_ed25519_autoskill`.
Read-only investigation first; two reversible ops changes applied, both backed up and recorded.
**No schema change, no application code change, no token rotation, no container was stopped or
killed.**

---

## Verdict

**The 502s were memory-cgroup OOM kills of the API container, caused by memory pressure from
concurrent ad-hoc package-hydration jobs running on the same 3.9 GB droplet — amplified by the
API running two uvicorn workers that each hold a full copy of the vector and lexical indexes
inside a single 1.5 GiB cgroup.**

Cloudflare returned 502 because the origin was genuinely dead each time the API process was
killed, not because of a tunnel fault.

## Evidence

### 1. Ten cgroup OOM kills, all of the API's python process

`dmesg -T | grep -c "Memory cgroup out of memory"` → **10**, all on 2026-08-02:

| time (UTC) | killed | anon-rss |
|---|---|---:|
| 03:42:18 | python | 1,027,796 kB |
| 03:45:55 | python | 1,010,904 kB |
| 03:46:16 | python | 1,030,424 kB |
| 03:52:42 | python | 1,040,736 kB |
| 03:52:53 | python | 1,032,856 kB |
| 04:00:15 | python | 1,029,796 kB |
| 04:02:25 | python | 974,412 kB |
| 04:18:35 | python | 972,984 kB |
| 04:25:33 | python | 1,035,768 kB |
| 04:27:55 | python | 1,019,724 kB |

Every record is `constraint=CONSTRAINT_MEMCG` with `uid:10001` — the API container's user. This
window (03:42–04:27 UTC) matches the period during which this session observed 502 on
`/healthz`, `/readyz` and `/skills-catalog`, and the later 200 responses match its end.

The kills are **memcg**, not host-OOM: the container hit its own limit.

### 2. The API cgroup is capped at 1.5 GiB and the process alone reaches ~1 GB

cgroup v2 (`cgroup2fs`). Live values for `deploy-api-1`:

```
memory.max          1610612736   (1.5 GiB, from compose `mem_limit: 1536m`)
memory.swap.max     1610612736
memory.current       927002624   (observed at rest, ~0.86 GiB)
memory.swap.current     892928   (only ~0.9 MB actually swapped)
```

Anon RSS at kill time was 0.97–1.04 GB. The remainder to 1.5 GiB is page cache from scanning the
2.2 GB SQLite, so `memory.current` crosses `memory.max` and the memcg OOMs even though anon alone
looks survivable.

`memory.events` currently reads `oom_kill 0` — but only because the container was recreated at
06:28 UTC, resetting the counters. `dmesg` is the durable record.

### 3. The API runs two workers, each warming its own full index

`docker logs deploy-api-1` shows every warm-up line **twice**:

```
[recommender] embedding model warmed in 0.8s
[recommender] embedding model warmed in 1.2s
[recommender] warmed local vector index (57637 vectors)
[recommender] warmed local vector index (57637 vectors)
[recommender] warmed lexical index (96963 skills, 85423 tokens)
[recommender] warmed lexical index (96963 skills, 85423 tokens)
```

Two workers × (vector matrix + lexical index + embedding model) in one 1.5 GiB cgroup. This is
the structural reason the ceiling is reachable at all.

Note the numbers: **96,963 skills in the lexical index** against 72,270 active in the 2026-08-01
snapshot. The corpus is actively growing right now, so per-worker memory is growing with it.

### 4. The trigger: concurrent hydration jobs, not the service itself

Alongside the 6 compose services there were **4–5 `deploy-hydrator-run-*` containers**, each with
a 1 GiB limit, created roughly one per 30–60 s:

```
python3 hydrate_github_packages.py --db /data/local_skills.db \
  --library-dir /app/skills_library --state /data/transient-retry.json \
  --limit 100 --workers 1 --retry-failed --only-pending
```

Combined hydrator usage was ~1.37 GiB. They are not in cron and not in any systemd timer, so they
are being driven by an operator session — **this is someone's live work and it was left running
and untouched.**

Declared compose limits already oversubscribe the box (api 1536m + collector 1024m + admin 768m +
library-backup 512m + mcp/litestream/cloudflared 256m each ≈ 4.6 GB on a 3.9 GB host). Adding
~5 GiB of hydrator allowance on top is what pushed the API over its cgroup ceiling.

### 5. Nothing was watching `/healthz`

No root cron entry, no user cron entry, no systemd timer referenced health. The outage was
invisible until a human hit the API. Docker log rotation is also unconfigured (no
`/etc/docker/daemon.json`); the largest container log is currently 7.2 MB, so this is a latent
issue rather than the cause. Disk is fine: 43 G used of 116 G (37%).

---

## Changes applied — both reversible, both backed up

Backup taken first: `/var/backups/autoskill-ops/fstab.bak-20260802T064219Z` and a full
before-state capture at `/var/backups/autoskill-ops/before-20260802T064219Z.txt`.

### Change 1 — host swap 2 GB → 4 GB

Rationale: the API cgroup already *permits* 1.5 GiB of swap (`memory.swap.max`) but was only
using 892 KB, because the host swapfile was 2 GB and already 1.1 GB consumed. Enlarging host swap
gives the kernel somewhere to push anon pages under pressure instead of OOM-killing.

```
fallocate -l 2G /swapfile2 && chmod 600 /swapfile2 && mkswap /swapfile2
swapon --priority -2 /swapfile2
echo "/swapfile2 none swap sw,pri=-2 0 0" >> /etc/fstab
```

The fstab entry was **verified by cycling it** (`swapoff /swapfile2 && swapon -a`) so a bad entry
could not surface later at boot.

| | before | after |
|---|---|---|
| swap devices | `/swapfile` 2 G | `/swapfile` 2 G + `/swapfile2` 2 G |
| swap total | 2,047 MB | **4,095 MB** |
| swap free | 941 MB | **2,822 MB** |
| disk free | 74 G | 72 G |

Revert: `sudo swapoff /swapfile2 && sudo sed -i '\#^/swapfile2#d' /etc/fstab && sudo rm /swapfile2`

### Change 2 — `/healthz` monitoring + log rotation for it

`/usr/local/bin/autoskill-healthz-probe.sh`, run every minute by root cron, appending to
`/var/log/autoskill-healthz.log`, with a 14-day `logrotate` policy.

It records local `/healthz`, edge `/healthz`, available memory, swap used, live hydrator count,
and the API container's `StartedAt`. **It is recording-only and deliberately takes no corrective
action** — an auto-restart would fight an operator who is intentionally loading the box.

First samples:

```
2026-08-02T06:42:59Z local=200 edge=200 mem_avail_mb=669 swap_used_mb=1173 hydrators=4 api_started=2026-08-02T06:40:46Z
2026-08-02T06:43:01Z local=200 edge=200 mem_avail_mb=551 swap_used_mb=1184 hydrators=4 api_started=2026-08-02T06:40:46Z
```

It immediately earned its keep: `api_started` moved from `06:28:54` to `06:40:46`, i.e. the API
was recreated again during this session. Next time there is an outage there will be a minute-by-
minute record of memory, swap and hydrator count leading into it.

Revert:
```
sudo crontab -l | grep -v autoskill-healthz-probe | sudo crontab -
sudo rm /usr/local/bin/autoskill-healthz-probe.sh /etc/logrotate.d/autoskill-healthz
```

---

## Deliberately NOT done (out of scope or unsafe right now)

- **Did not stop or throttle the hydrators.** They are an operator's live work, not ops config.
- **Did not raise the API `mem_limit`.** The host is already oversubscribed; giving the API more
  headroom on a 3.9 GB box would just move the OOM to another service.
- **Did not reduce the API to one uvicorn worker**, which is the real structural fix for the
  double-index memory cost. It requires recreating the API container and would have interrupted
  the in-flight hydration.
- **Did not configure Docker daemon log rotation.** Writing `/etc/docker/daemon.json` needs a
  daemon restart, which would bounce every container including the operator's hydrators.

## Recommendations for Sami

1. **Drop the API to a single uvicorn worker**, or make the vector/lexical index shared rather
   than per-worker. Two workers each holding a full copy of a *growing* corpus inside a fixed
   1.5 GiB cgroup is a ceiling that will be hit again as the corpus grows — and it is growing
   (96,963 skills in the lexical index today).
2. **Do not run hydration concurrently with serving on this droplet**, or cap it to one worker
   at a time. Collection was moved off-host in July for exactly this reason.
3. **Add `/etc/docker/daemon.json`** with `log-driver: json-file`, `max-size: 10m`, `max-file: 3`,
   applied during a planned restart window.
4. Consider a larger droplet if serving and hydration must coexist: 3.9 GB with ~4.6 GB of
   declared limits has no margin.
