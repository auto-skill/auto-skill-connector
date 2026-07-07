"""Live smoke test for the public Auto-Skill connector path.

This intentionally talks to the configured public service and should not run
in unit-test CI. It is for launch/release checks:

    python scripts/live_smoke.py

Set AUTOSKILL_URL to test a staging host.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from auto_skill_core import get_autoskill_url, route_prompt_payload, route_task_payload


CHECKS = [
    {
        "name": "spreadsheet full route",
        "task": "create an excel spreadsheet report with formulas and charts",
        "min_tier": "full",
        "must_include": ("spreadsheet", "formula"),
    },
    {
        "name": "generic landing page avoids Landingi trap",
        "task": "build a landing page for an AI automation agency",
        "min_tier": "hint",
        "must_not_include": ("landingi",),
    },
]

TIER_ORDER = {"none": 0, "hint": 1, "full": 2}


def _skill_blob(payload: dict) -> str:
    skill = payload.get("selected_skill") or {}
    return " ".join(
        str(skill.get(key) or "")
        for key in ("name", "description", "url", "source")
    ).lower()


def _content_blob(payload: dict) -> str:
    return f"{_skill_blob(payload)} {payload.get('skill_content') or ''}".lower()


async def main() -> int:
    url = get_autoskill_url()
    print(f"autoskill_url={url}")

    failures: list[str] = []
    async with httpx.AsyncClient() as client:
        skip = await route_prompt_payload("ok", client=client)
        if skip.get("should_route") is False:
            print("[PASS] prompt preflight skips acknowledgements")
        else:
            failures.append("prompt preflight routed a tiny acknowledgement")
            print(f"[FAIL] prompt preflight: {skip}")

        for check in CHECKS:
            payload = await route_task_payload(check["task"], client=client)
            tier = payload.get("route_tier") or "none"
            blob = _content_blob(payload)
            skill = payload.get("selected_skill") or {}
            label = skill.get("name") or "<none>"
            backend = payload.get("search_backend")

            ok = payload.get("routed") is True
            ok = ok and TIER_ORDER.get(tier, 0) >= TIER_ORDER[check["min_tier"]]
            ok = ok and all(word in blob for word in check.get("must_include", ()))
            ok = ok and not any(word in blob for word in check.get("must_not_include", ()))
            if check["min_tier"] == "full":
                ok = ok and bool(payload.get("skill_content"))

            if ok:
                print(f"[PASS] {check['name']}: tier={tier}, skill={label}, backend={backend}")
            else:
                failures.append(check["name"])
                print(f"[FAIL] {check['name']}: tier={tier}, skill={label}, backend={backend}")
                print(f"       message={payload.get('message')}")
                print(f"       warnings={payload.get('warnings')}")

    if failures:
        print(f"live_smoke: {len(failures)} failure(s): {', '.join(failures)}")
        return 1
    print("live_smoke: passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
