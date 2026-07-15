from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cleanup_scrape_runs


class CleanupScrapeRunsTests(unittest.TestCase):
    def test_retire_all_closes_even_freshest_running_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "runs.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE scrape_runs (id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, error TEXT)"
            )
            conn.execute(
                "INSERT INTO scrape_runs (id,started_at,status) VALUES (?,?,?)",
                ("fresh", "2099-01-01T00:00:00+00:00", "running"),
            )
            conn.commit()
            conn.close()

            with (
                patch.object(cleanup_scrape_runs, "DB_PATH", db_path),
                patch.object(cleanup_scrape_runs, "init_db", return_value=None),
            ):
                result = cleanup_scrape_runs.main(["--apply", "--retire-all"])

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT status,error FROM scrape_runs WHERE id='fresh'").fetchone()
            conn.close()
            self.assertEqual(result, 0)
            self.assertEqual(row[0], "stale")
            self.assertIn("production scraping retired", row[1])


if __name__ == "__main__":
    unittest.main()
