"""Live smoke test for the public Auto-Skill connector path.

This intentionally talks to the configured public service and should not run
in unit-test CI. It is for launch/release checks:

    python scripts/live_smoke.py

Set AUTOSKILL_URL to test a staging backend.
Set AUTOSKILL_MCP_HEALTH_URL to test a non-default remote MCP health endpoint.
"""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlparse

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
DEFAULT_MCP_HEALTH_URL = "https://mcp.auto-skill.dev/healthz"


def get_mcp_health_url() -> str:
    configured = (os.getenv("AUTOSKILL_MCP_HEALTH_URL") or DEFAULT_MCP_HEALTH_URL).strip()
    parsed = urlparse(configured)
    if parsed.path.rstrip("/") == "/mcp":
        return configured[: -len(parsed.path)] + "/healthz"
    return configured


def _skill_blob(payload: dict) -> str:
    skill = payload.get("selected_skill") or {}
    return " ".join(
        str(skill.get(key) or "")
        for key in ("name", "description", "url", "source")
    ).lower()


def _content_blob(payload: dict) -> str:
    return f"{_skill_blob(payload)} {payload.get('skill_content') or ''}".lower()


def validate_route_payload(check: dict, payload: dict) -> tuple[bool, list[str]]:
    failures: list[str] = []
    tier = payload.get("route_tier") or "none"
    blob = _content_blob(payload)

    if payload.get("routed") is not True:
        failures.append("not routed")
    if TIER_ORDER.get(tier, 0) < TIER_ORDER[check["min_tier"]]:
        failures.append(f"tier {tier!r} below required {check['min_tier']!r}")
    missing_words = [word for word in check.get("must_include", ()) if word not in blob]
    if missing_words:
        failures.append(f"missing expected word(s): {', '.join(missing_words)}")
    forbidden_words = [word for word in check.get("must_not_include", ()) if word in blob]
    if forbidden_words:
        failures.append(f"included forbidden word(s): {', '.join(forbidden_words)}")
    if check["min_tier"] == "full" and not payload.get("skill_content"):
        failures.append("full route did not include skill_content")

    summary = payload.get("route_summary") if isinstance(payload.get("route_summary"), dict) else {}
    expected_decision = "apply_skill_content" if tier == "full" else "consider_hint" if tier == "hint" else "continue_normally"
    if summary.get("decision") != expected_decision:
        failures.append(f"route_summary decision {summary.get('decision')!r} != {expected_decision!r}")
    if not summary.get("selected_name") and tier in {"full", "hint"}:
        failures.append("route_summary missing selected_name")

    metrics = payload.get("route_metrics") if isinstance(payload.get("route_metrics"), dict) else {}
    for key in ("latency_ms", "skill_find_ms", "injected_tokens", "response_tokens"):
        if not isinstance(metrics.get(key), (int, float)):
            failures.append(f"route_metrics missing {key}")
    summary_metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    if metrics and not summary_metrics:
        failures.append("route_summary missing compact metrics")

    return not failures, failures


async def main() -> int:
    url = get_autoskill_url()
    print(f"autoskill_url={url}")

    failures: list[str] = []
    async with httpx.AsyncClient() as client:
        mcp_health_url = get_mcp_health_url()
        try:
            mcp_response = await client.get(mcp_health_url, timeout=10)
            if mcp_response.headers.get("content-type", "").startswith("application/json"):
                mcp_body = mcp_response.json()
            else:
                mcp_body = {}
        except Exception as exc:
            mcp_response = None
            mcp_body = {}
            failures.append("remote MCP health")
            print(f"[FAIL] remote MCP health: {exc}")
        if mcp_response is not None:
            if mcp_response.status_code == 200 and mcp_body.get("ok") is True:
                print(f"[PASS] remote MCP health: {mcp_health_url}")
            else:
                failures.append("remote MCP health")
                print(f"[FAIL] remote MCP health: status={mcp_response.status_code}, body={mcp_body}")

        skip = await route_prompt_payload("ok", client=client)
        if skip.get("should_route") is False:
            print("[PASS] prompt preflight skips acknowledgements")
        else:
            failures.append("prompt preflight routed a tiny acknowledgement")
            print(f"[FAIL] prompt preflight: {skip}")

        for check in CHECKS:
            payload = await route_task_payload(check["task"], client=client)
            tier = payload.get("route_tier") or "none"
            skill = payload.get("selected_skill") or {}
            label = skill.get("name") or "<none>"
            backend = payload.get("search_backend")

            ok, route_failures = validate_route_payload(check, payload)

            if ok:
                print(f"[PASS] {check['name']}: tier={tier}, skill={label}, backend={backend}")
            else:
                failures.append(check["name"])
                print(f"[FAIL] {check['name']}: tier={tier}, skill={label}, backend={backend}")
                print(f"       contract_failures={route_failures}")
                print(f"       message={payload.get('message')}")
                print(f"       warnings={payload.get('warnings')}")

    if failures:
        print(f"live_smoke: {len(failures)} failure(s): {', '.join(failures)}")
        return 1
    print("live_smoke: passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
