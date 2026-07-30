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
        return [{"id": "acme/skills/official", "skills_sh_id": "acme/skills/official", "mirror_fresh": True}]

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
