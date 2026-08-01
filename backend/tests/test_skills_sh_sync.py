from __future__ import annotations

import asyncio
from argparse import Namespace

import sync_skills_sh_mirror as sync


class _FakeCatalog:
    configured = True
    _mirror = None

    async def curated(self):
        return [{"id": "acme/skills/official", "name": "Official"}]

    async def leaderboard(self, **kwargs):
        return [
            {"id": "acme/skills/trending", "name": "Trending"},
            {"id": "acme/skills/official", "name": "Official"},
        ]

    async def cached_ids(self, ids):
        return [{
            "id": "acme/skills/official",
            "skills_sh_id": "acme/skills/official",
            "mirror_fresh": True,
            "quality_status": "active",
            "content_hash": "a" * 64,
            "_source_files_complete": True,
        }]

    async def hydrate_listings(self, listings, limit):
        return [{"id": row["id"], "quality_status": "active"} for row in listings]


def test_sync_is_bounded_resumable_and_prefers_curated(monkeypatch):
    monkeypatch.setattr(sync, "default_catalog", lambda: _FakeCatalog())
    args = Namespace(
        view="trending",
        pages=1,
        per_page=50,
        max_skills=2,
        batch_size=1,
        delay_seconds=0,
        no_curated=False,
    )
    result = asyncio.run(sync.sync_mirror(args))
    assert result["selected"] == 2
    assert result["already_fresh"] == 1
    assert result["pending"] == 1
    assert result["hydrated"] == 1


def test_sync_rehydrates_active_row_with_incomplete_source_files(monkeypatch):
    class IncompleteCatalog(_FakeCatalog):
        async def cached_ids(self, ids):
            rows = await super().cached_ids(ids)
            rows[0]["_source_files_complete"] = False
            return rows

    monkeypatch.setattr(sync, "default_catalog", lambda: IncompleteCatalog())
    args = Namespace(
        view="trending",
        pages=1,
        per_page=50,
        max_skills=2,
        batch_size=2,
        delay_seconds=0,
        no_curated=False,
    )
    result = asyncio.run(sync.sync_mirror(args))
    assert result["already_fresh"] == 0
    assert result["pending"] == 2
    assert result["hydrated"] == 2


def test_all_listings_indexes_every_page_but_hydrates_only_bounded_top(monkeypatch):
    class FullCatalog(_FakeCatalog):
        def __init__(self):
            self.indexed = []

        async def curated(self):
            return []

        async def leaderboard_page(self, **kwargs):
            page = kwargs["page"]
            if page == 0:
                return {
                    "data": [{"id": "acme/skills/one", "name": "One"}],
                    "pagination": {"hasMore": True},
                }
            return {
                "data": [
                    {"id": "acme/skills/two", "name": "Two"},
                    {"id": "acme/skills/one", "name": "One"},
                ],
                "pagination": {"hasMore": False},
            }

        async def index_listings(self, rows):
            self.indexed.extend(row["id"] for row in rows)
            return len(rows)

    catalog = FullCatalog()
    monkeypatch.setattr(sync, "default_catalog", lambda: catalog)
    args = Namespace(
        view="all-time",
        pages=0,
        per_page=500,
        max_skills=100,
        all_listings=True,
        hydrate_top=1,
        batch_size=1,
        delay_seconds=0,
        no_curated=True,
    )
    result = asyncio.run(sync.sync_mirror(args))
    assert result["pages_fetched"] == 2
    assert result["metadata_indexed"] == 3
    assert result["selected"] == 1
    assert result["hydrated"] == 1
    assert catalog.indexed == ["acme/skills/one", "acme/skills/two", "acme/skills/one"]


def test_all_listings_can_resume_across_every_indexed_listing(monkeypatch):
    class FullCatalog(_FakeCatalog):
        def __init__(self):
            self.hydrated_ids = []

        async def curated(self):
            return []

        async def leaderboard_page(self, **kwargs):
            return {
                "data": [{"id": f"acme/skills/{index}"} for index in range(3)],
                "pagination": {"hasMore": False},
            }

        async def cached_ids(self, ids):
            return []

        async def index_listings(self, rows):
            return len(rows)

        async def hydrate_listings(self, listings, limit):
            self.hydrated_ids.extend(row["id"] for row in listings)
            return [{"id": row["id"], "quality_status": "active"} for row in listings]

    catalog = FullCatalog()
    monkeypatch.setattr(sync, "default_catalog", lambda: catalog)
    args = Namespace(
        view="all-time",
        pages=0,
        per_page=500,
        max_skills=100,
        all_listings=True,
        hydrate_all=True,
        hydrate_top=1,
        batch_size=2,
        delay_seconds=0,
        no_curated=True,
    )
    result = asyncio.run(sync.sync_mirror(args))
    assert result["hydrate_all"] is True
    assert result["selected"] == 3
    assert result["hydrated"] == 3
    assert catalog.hydrated_ids == ["acme/skills/0", "acme/skills/1", "acme/skills/2"]


def test_sync_skips_terminal_failures_until_explicit_retry(monkeypatch):
    class LedgerCatalog(_FakeCatalog):
        def __init__(self):
            self.hydrated_ids = []

        async def curated(self):
            return []

        async def leaderboard(self, **kwargs):
            return [{"id": "acme/skills/terminal"}, {"id": "acme/skills/retry"}]

        async def cached_ids(self, ids):
            return []

        async def latest_ingestion_attempt(self, skill_id, snapshot_hash):
            if skill_id.endswith("terminal"):
                return {"retryable": 0, "status": "rejected"}
            return None

        async def hydrate_listings(self, listings, limit):
            self.hydrated_ids.extend(row["id"] for row in listings)
            return [{"id": row["id"], "quality_status": "active"} for row in listings]

    catalog = LedgerCatalog()
    monkeypatch.setattr(sync, "default_catalog", lambda: catalog)
    args = Namespace(
        view="trending",
        pages=1,
        per_page=50,
        max_skills=10,
        all_listings=False,
        hydrate_all=False,
        hydrate_top=100,
        retry_failed=False,
        batch_size=2,
        delay_seconds=0,
        no_curated=False,
    )
    result = asyncio.run(sync.sync_mirror(args))
    assert result["terminal_skipped"] == 1
    assert catalog.hydrated_ids == ["acme/skills/retry"]
