import sqlite3

from local_store import DB_PATH

conn = sqlite3.connect(DB_PATH)
print(conn.execute(
    "select count(*) from skills where quality_status='active' "
    "and (capability_summary is null or capability_summary='')"
).fetchone()[0])
