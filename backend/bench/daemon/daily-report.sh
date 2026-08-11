#!/bin/bash
# Daily ingestion delta to the codex-autoskill thread, computed from the
# analytics snapshots so the numbers match what any graph will show.
source /srv/mobile-codex/autoskill-daemon/bin/common.sh
cd "$BENCH" || exit 1
python3 - <<'PY' > /tmp/daily_report.txt
import sqlite3, datetime
SNAP="/srv/mobile-codex/sessions/autoskill_7e0dd3fa/workspace/auto-skill-connector/backend/analytics_v1.db"
c=sqlite3.connect(f"file:{SNAP}?mode=ro",uri=True)
rows=c.execute("select ts,sightings_skillmd,distinct_skill_shas,judged_unique,"
               "inherited,included,packages,luna_tokens_in_day from metrics_snapshots"
               " where source='live' order by ts").fetchall()
if not rows: raise SystemExit
now=rows[-1]
day_ago=datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=24)
base=rows[0]
for r in rows:
    if r[0] >= day_ago.isoformat(): break
    base=r
def d(i): return (now[i] or 0)-(base[i] or 0)
print(f""":bar_chart: **AutoSkill daily ingestion report**
sightings discovered : {now[1]:,} (+{d(1):,})
unique skill shas    : {now[2]:,} (+{d(2):,})
judged unique        : {now[3]:,} (+{d(3):,})
included             : {now[5]:,}
servable packages    : {now[6]:,} (+{d(6):,})
verdicts inherited   : {now[4]:,} (+{d(4):,})
Luna tokens today    : {now[7]:,}
CSV: `run2_metrics.py --export ingestion.csv`""")
PY
[[ -s /tmp/daily_report.txt ]] && notify "$(cat /tmp/daily_report.txt)"
