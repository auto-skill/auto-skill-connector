"""Command line interface for auto-skill."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from auto_skill_auth import clear_credentials, save_credentials
from auto_skill_core import (
    AutoSkillError,
    NotLoggedInError,
    SkillAlreadyExistsError,
    UnsupportedTargetError,
    _fetch_content,
    _search,
    add_favorite,
    get_autoskill_url,
    get_impact_report,
    get_measurement_mode,
    get_skills_home,
    install_skill_from_content,
    is_url,
    list_favorites,
    list_private_skills,
    logout_backend,
    recommend_skill_payload,
    record_route_feedback,
    record_route_survey_response,
    remove_favorite,
    remove_private_skill,
    route_prompt_payload,
    route_task_payload,
    set_measurement_mode,
    submit_private_skill,
    validate_skill_content,
    whoami,
)
from auto_skill_receipt import format_route_receipt
from auto_skill_mining import (
    get_mined_skill,
    get_mined_skills_dir,
    list_local_sessions,
    list_mined_skills,
    publish_mined_skill,
    remove_mined_skill,
    save_mined_skill,
)
from auto_skill_personalize import reset_weights, weights_summary

HOOK_SCRIPT_PATH = Path(__file__).resolve().parent / "hooks" / "skill_suggest.py"


def _default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _print_warning_lines(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _short(value: str | None, limit: int = 140) -> str:
    text = (value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _print_candidate(index: int, candidate: dict[str, Any]) -> None:
    name = candidate.get("name") or "unknown"
    print(f"{index}. {name}")
    if candidate.get("description"):
        print(f"   {_short(candidate.get('description'))}")
    details = []
    if candidate.get("stars") is not None:
        details.append(f"stars={candidate.get('stars')}")
    if candidate.get("risk_score") is not None:
        details.append(f"risk={candidate.get('risk_score')}")
    if details:
        print(f"   {', '.join(details)}")
    if candidate.get("url"):
        print(f"   {candidate.get('url')}")


def _print_route_metrics(payload: dict[str, Any]) -> None:
    metrics = payload.get("route_metrics")
    if not isinstance(metrics, dict) or not metrics:
        return
    parts = []
    for key, label in (
        ("latency_ms", "latency"),
        ("skill_find_ms", "skill_find"),
        ("injected_tokens", "injected_tokens"),
        ("response_tokens", "response_tokens"),
    ):
        value = metrics.get(key)
        if isinstance(value, (int, float)):
            unit = "ms" if key.endswith("_ms") else ""
            parts.append(f"{label}={int(value)}{unit}")
    if parts:
        print(f"metrics: {', '.join(parts)}")


def _print_route_summary(payload: dict[str, Any]) -> None:
    summary = payload.get("route_summary")
    if not isinstance(summary, dict) or not summary:
        return
    decision = summary.get("decision")
    reason = summary.get("reason")
    selected = summary.get("selected_name")
    parts = [f"decision={decision}"] if decision else []
    if selected:
        parts.append(f"selected={selected}")
    if reason:
        parts.append(str(reason))
    if parts:
        print(f"summary: {'; '.join(parts)}")


async def _command_search(args: argparse.Namespace) -> int:
    task = " ".join(args.task).strip()
    async with httpx.AsyncClient() as client:
        result = await _search(client, task)

    _print_warning_lines(result.get("warnings", []))
    print(f"backend: {result.get('search_backend', 'unknown')}")

    if result.get("type") == "none":
        print(result.get("message", "No matching skill found."))
        return 1

    if result.get("type") == "clarify":
        print(result.get("message", "A few skills matched."))
        for index, candidate in enumerate(result.get("options") or [], start=1):
            _print_candidate(index, candidate)
        return 0

    skill = result.get("skill") or {}
    print("best match:")
    _print_candidate(1, skill)
    print()
    print(f'preview: auto-skill preview "{task}"')
    return 0


async def _command_route(args: argparse.Namespace) -> int:
    task = " ".join(args.task).strip()
    async with httpx.AsyncClient() as client:
        payload = await route_task_payload(task, client=client)

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0 if payload.get("routed") else 1

    _print_warning_lines(payload.get("warnings", []))
    print(f"backend: {payload.get('search_backend', 'unknown')}")
    print(f"route: {payload.get('route_type')}")
    if payload.get("route_tier"):
        print(f"tier: {payload.get('route_tier')}")
    _print_route_summary(payload)
    _print_route_metrics(payload)

    if not payload.get("routed"):
        print(payload.get("message", "No matching skill found."))
        return 1

    receipt = payload.get("route_receipt") or format_route_receipt(payload)
    if receipt:
        print(receipt)
        print()

    plan = payload.get("skill_plan") if isinstance(payload.get("skill_plan"), dict) else {}
    policy_skills = plan.get("policy_skills") or []
    primary_skill = plan.get("primary_skill") if isinstance(plan.get("primary_skill"), dict) else None
    if not receipt and (policy_skills or primary_skill):
        print(f"skill plan ({plan.get('task_family') or 'general'}):")
        for policy in policy_skills:
            if isinstance(policy, dict):
                print(f"  policy: {policy.get('name') or 'unknown'}")
        if primary_skill:
            print(f"  primary: {primary_skill.get('name') or 'unknown'}")
        print()

    skill = payload.get("selected_skill") or {}
    print("compatibility selection:")
    _print_candidate(1, skill)
    print()
    if payload.get("route_type") == "hint":
        candidates = payload.get("candidates") or []
        if candidates:
            print("candidate options:")
            for index, candidate in enumerate(candidates[:3], start=1):
                _print_candidate(index, candidate)
            print()
        print("next action: treat this as a suggestion; do not inject full skill content")
        return 0

    print("next action: apply the returned skill plan in-turn; no skill installation is required")
    if args.show_content:
        print()
        content = payload.get("skill_content") or ""
        if args.max_chars and len(content) > args.max_chars:
            print(content[: args.max_chars])
            print(f"\n[truncated to {args.max_chars} characters]")
        else:
            print(content)
    return 0


async def _command_route_prompt(args: argparse.Namespace) -> int:
    prompt = " ".join(args.prompt).strip()
    async with httpx.AsyncClient() as client:
        payload = await route_prompt_payload(prompt, client=client)

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0 if payload.get("should_route") else 1

    if not payload.get("should_route"):
        print(f"skip: {payload.get('reason')}")
        return 1

    route = payload.get("route") or {}
    _print_warning_lines(route.get("warnings", []))
    if not payload.get("routed"):
        print(route.get("message", "No matching skill found."))
        return 1

    if args.context_only:
        print(payload.get("context", ""))
        return 0

    print(f"preflight: {payload.get('reason')}")
    print(f"backend: {route.get('search_backend', 'unknown')}")
    skill = route.get("selected_skill") or {}
    print("selected skill:")
    _print_candidate(1, skill)
    print()
    candidates = route.get("candidates") or []
    if route.get("route_type") == "hint" and candidates:
        print("candidate options:")
        for index, candidate in enumerate(candidates[:3], start=1):
            _print_candidate(index, candidate)
        print()
    print("injectable context:")
    print(payload.get("context", ""))
    return 0


async def _command_feedback(args: argparse.Namespace) -> int:
    ok = await record_route_feedback(
        args.route_id,
        args.outcome,
        source="auto-skill-cli",
        note=args.note,
    )
    if ok:
        print(f"recorded feedback: route_id={args.route_id} outcome={args.outcome}")
        return 0
    print("feedback was not recorded")
    return 1


async def _command_survey(args: argparse.Namespace) -> int:
    ok = await record_route_survey_response(args.response)
    if ok:
        print(f"recorded survey response: {args.response}")
        return 0
    print("survey response was not recorded (not logged in, or an invalid value)")
    return 1


def _print_impact_report(report: dict[str, Any]) -> None:
    activity = report.get("activity") or {}
    efficiency = report.get("context_efficiency") or {}
    lift = report.get("measured_lift") or {}

    print(f"Auto-Skill impact report -- last {report.get('window_days', '?')} days")
    print()
    print("What Auto-Skill did for you:")
    print(f"  substantial tasks routed: {activity.get('substantial_tasks_routed', 0)}")
    tiers = activity.get("tiers") or {}
    if tiers:
        tiers_text = ", ".join(f"{tier}={count}" for tier, count in sorted(tiers.items()))
        print(f"  tiers: {tiers_text}")
    print(f"  declined uncertain matches (trust behavior): {activity.get('declined_uncertain_count', 0)}")
    families = activity.get("top_task_families") or []
    if families:
        families_text = ", ".join(f"{f['task_family']} ({f['count']})" for f in families[:5])
        print(f"  top task families: {families_text}")
    print(
        "  distinct specialists delivered: "
        f"{activity.get('distinct_specialists_delivered', 0)} "
        f"({activity.get('specialists_discovered_without_install', 0)} discovered without installing anything)"
    )
    print()
    print("Context efficiency:")
    raw = efficiency.get("eligible_raw_tokens") or 0
    delivered = efficiency.get("delivered_capsule_tokens") or 0
    ratio = efficiency.get("compression_ratio")
    if raw:
        pct = round((ratio or 0) * 100)
        print(
            f"  delivered {delivered:,} tokens of task-specific guidance from {raw:,} tokens of verified "
            f"source material: a {pct}% reduction in reference context"
        )
    else:
        print("  no full-tier deliveries in this window yet")
    if efficiency.get("median_injected_tokens") is not None:
        print(
            f"  injected tokens: median={efficiency['median_injected_tokens']}, "
            f"p95={efficiency.get('p95_injected_tokens')}, budget={efficiency.get('token_budget')}"
        )
    if efficiency.get("budget_compliance_rate") is not None:
        print(f"  within budget: {round(efficiency['budget_compliance_rate'] * 100)}%")
    print()
    print("Proven lift:")
    if lift.get("available"):
        print(
            f"  in comparable measured tasks, routed guidance changed median {lift['metric']} by "
            f"{lift['relative_reduction_pct']}% (95% CI: {lift['ci_low_pct']}% to {lift['ci_high_pct']}%; "
            f"{lift['measured_tasks']} measured tasks)"
        )
    else:
        reason = lift.get("reason", "not_enough_measurement_mode_data")
        if reason == "measurement_mode_not_enabled":
            print("  not available -- opt in with `auto-skill measurement-mode enable` to start measuring.")
        else:
            print("  we have not collected enough controlled observations to claim a personal performance lift yet.")


async def _command_impact_report(args: argparse.Namespace) -> int:
    try:
        report = await get_impact_report(args.days)
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    _print_impact_report(report)
    return 0


async def _command_measurement_mode_status(args: argparse.Namespace) -> int:
    del args
    try:
        settings = await get_measurement_mode()
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    state = "enabled" if settings.get("enabled") else "disabled"
    print(f"measurement mode: {state} (holdout_rate={settings.get('holdout_rate')})")
    return 0


async def _command_measurement_mode_enable(args: argparse.Namespace) -> int:
    try:
        settings = await set_measurement_mode(True, args.holdout_rate)
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"measurement mode enabled (holdout_rate={settings.get('holdout_rate')}). "
        "A small share of otherwise-full-tier tasks may not receive automated guidance, "
        "so their outcomes can be compared against routed tasks."
    )
    return 0


async def _command_measurement_mode_disable(args: argparse.Namespace) -> int:
    del args
    try:
        await set_measurement_mode(False)
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("measurement mode disabled")
    return 0


def _print_route_metrics_summary(payload: dict[str, Any]) -> None:
    window = payload.get("window_hours")
    total = int(payload.get("total") or 0)
    print(f"window_hours: {window}")
    print(f"routes: {total}")
    print(
        "p95: "
        f"latency={int(payload.get('p95_latency_ms') or 0)}ms, "
        f"skill_find={int(payload.get('p95_skill_find_ms') or 0)}ms, "
        f"injected_tokens={int(payload.get('p95_injected_tokens') or 0)}, "
        f"response_tokens={int(payload.get('p95_response_tokens') or 0)}"
    )
    breaches = payload.get("budget_breaches") if isinstance(payload.get("budget_breaches"), dict) else {}
    print(
        "budget_breaches: "
        f"any={int(breaches.get('any') or 0)}, "
        f"latency={int(breaches.get('latency_ms') or 0)}, "
        f"skill_find={int(breaches.get('skill_find_ms') or 0)}, "
        f"injected_tokens={int(breaches.get('injected_tokens') or 0)}, "
        f"response_tokens={int(breaches.get('response_tokens') or 0)}"
    )
    tiers = payload.get("tiers") if isinstance(payload.get("tiers"), dict) else {}
    outcomes = payload.get("outcomes") if isinstance(payload.get("outcomes"), dict) else {}
    if tiers:
        print("tiers: " + ", ".join(f"{key}={value}" for key, value in sorted(tiers.items())))
    if outcomes:
        print("outcomes: " + ", ".join(f"{key}={value}" for key, value in sorted(outcomes.items())))
    top_skills = payload.get("top_skills") if isinstance(payload.get("top_skills"), list) else []
    if top_skills:
        print("top skills:")
        for skill in top_skills[:5]:
            name = skill.get("skill_name") or "unknown"
            count = int(skill.get("count") or 0)
            positives = int(skill.get("positive_count") or 0)
            avg_find = int(skill.get("avg_skill_find_ms") or 0)
            avg_tokens = int(skill.get("avg_injected_tokens") or 0)
            print(f"- {name}: count={count}, positive={positives}, avg_find={avg_find}ms, avg_injected={avg_tokens}")


async def _command_metrics(args: argparse.Namespace) -> int:
    base_url = (args.base_url or get_autoskill_url()).rstrip("/")
    if not base_url:
        print("error: AUTOSKILL_URL is not configured; pass --base-url for a local API", file=sys.stderr)
        return 1
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{base_url}/route-metrics",
                params={"hours": str(args.hours)},
                headers={"Accept": "application/json"},
                timeout=10,
            )
    except Exception as exc:
        print(f"error: route metrics unavailable ({exc})", file=sys.stderr)
        return 1
    if response.status_code == 403:
        print("error: /route-metrics is local-only; point --base-url at the host loopback API", file=sys.stderr)
        return 2
    if response.status_code != 200:
        print(f"error: /route-metrics returned HTTP {response.status_code}: {response.text[:300]}", file=sys.stderr)
        return 1
    payload = response.json()
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    _print_route_metrics_summary(payload)
    breaches = payload.get("budget_breaches") if isinstance(payload.get("budget_breaches"), dict) else {}
    return 1 if int(breaches.get("any") or 0) else 0


async def _command_validate(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"error: {path} is not a file", file=sys.stderr)
        return 1
    result = validate_skill_content(path.read_text(encoding="utf-8"))
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    for error in result["errors"]:
        print(f"error: {error}", file=sys.stderr)
    _print_warning_lines(result["warnings"])
    if result["ok"]:
        print(f"ok: {result['name']} (installs as {result['slug']}/SKILL.md)")
        return 0
    print("invalid: fix the errors above and validate again", file=sys.stderr)
    return 1


async def _resolve_cli_skill(source: str) -> dict[str, Any]:
    local_path = Path(source)
    if not is_url(source) and local_path.is_file():
        content = local_path.read_text(encoding="utf-8")
        result = validate_skill_content(content)
        if not result["ok"]:
            details = "; ".join(result["errors"])
            raise AutoSkillError(f"{source} failed validation: {details}")
        return {
            "content": content,
            "source_url": local_path.resolve().as_uri(),
            "metadata": {"path": str(local_path)},
            "warnings": result["warnings"],
        }
    async with httpx.AsyncClient() as client:
        if is_url(source):
            content = await _fetch_content(client, source)
            if not content:
                raise AutoSkillError(f"Could not fetch content for {source}")
            return {
                "content": content,
                "source_url": source,
                "metadata": {"url": source},
                "warnings": [],
            }

        payload = await recommend_skill_payload(source, client=client)
        if payload.get("warnings"):
            _print_warning_lines(payload.get("warnings", []))
        if not payload.get("found"):
            candidates = payload.get("candidates") or []
            if candidates:
                print(payload.get("message", "Multiple skills matched."))
                for index, candidate in enumerate(candidates, start=1):
                    _print_candidate(index, candidate)
                raise AutoSkillError("Search was ambiguous. Run install with a specific source URL or a more specific task.")
            raise AutoSkillError(payload.get("message", "No matching skill found."))

        metadata = payload.get("best_match") or {}
        return {
            "content": payload.get("skill_content") or "",
            "source_url": metadata.get("url") or source,
            "metadata": metadata,
            "warnings": payload.get("warnings", []),
        }


async def _command_preview(args: argparse.Namespace) -> int:
    source = " ".join(args.source).strip()
    try:
        resolved = await _resolve_cli_skill(source)
    except AutoSkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    metadata = resolved["metadata"]
    if metadata.get("name"):
        print(f"name: {metadata.get('name')}")
    print(f"source: {resolved['source_url']}")
    if metadata.get("stars") is not None:
        print(f"stars: {metadata.get('stars')}")
    if metadata.get("risk_score") is not None:
        print(f"risk: {metadata.get('risk_score')}")
    print()

    content = resolved["content"]
    max_chars = args.max_chars
    if max_chars and len(content) > max_chars:
        print(content[:max_chars])
        print(f"\n[truncated to {max_chars} characters]")
    else:
        print(content)
    return 0


def _install_capability_warnings(content: str) -> list[str]:
    checks = (
        ("declares agent tool permissions", r"(?im)^\s*allowed-tools\s*:"),
        ("references bundled scripts", r"(?i)(?:^|[\s`(])scripts[/\\]"),
        ("references bundled assets or documentation", r"(?i)(?:^|[\s`(])(?:assets|references)[/\\]"),
        ("mentions network access", r"(?i)\b(?:curl|wget|httpx|requests|fetch)\b|https?://"),
        ("installs external dependencies", r"(?i)\b(?:pip|npm|pnpm|yarn|uv|brew|apt(?:-get)?)\s+(?:install|add)\b"),
        ("mentions elevated or destructive commands", r"(?i)\b(?:sudo|rm\s+-rf|chmod\s+777|powershell\s+-enc)\b"),
    )
    return [label for label, pattern in checks if re.search(pattern, content)]


def _confirm_install(dest_hint: Path, warnings: list[str]) -> bool:
    if not sys.stdin.isatty():
        print("error: refusing non-interactive install without --yes", file=sys.stderr)
        return False
    qualifier = "unverified skill with capability warnings" if warnings else "unverified instruction-only skill"
    answer = input(f"Install this {qualifier} to {dest_hint}? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


async def _command_install(args: argparse.Namespace) -> int:
    source = " ".join(args.source).strip()
    try:
        resolved = await _resolve_cli_skill(source)
        home = get_skills_home(args.target)
        preview_result = install_skill_from_content(
            resolved["content"],
            source_url=resolved["source_url"],
            name=args.name or "",
            target=args.target,
            skills_home=home,
            force=args.force,
            dry_run=True,
        )
    except (AutoSkillError, UnsupportedTargetError, SkillAlreadyExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"source: {preview_result['source_url']}")
    print(f"target: {preview_result['target']}")
    print(f"destination: {preview_result['dest_file']}")
    print(f"overwrite: {'yes' if preview_result['would_overwrite'] else 'no'}")
    print(f"content sha256: {hashlib.sha256(resolved['content'].encode('utf-8')).hexdigest()}")
    print("publisher identity: unverified")
    capability_warnings = _install_capability_warnings(resolved["content"])
    for warning in capability_warnings:
        print(f"capability warning: {warning}")
    print("install policy: explicit local install; no automatic persistence or trust-policy bypass")

    if args.dry_run:
        print("dry run: no files written")
        return 0

    if not args.yes and not _confirm_install(preview_result["dest_file"], capability_warnings):
        print("cancelled")
        return 1

    try:
        result = install_skill_from_content(
            resolved["content"],
            source_url=resolved["source_url"],
            name=args.name or "",
            target=args.target,
            skills_home=home,
            force=args.force,
            dry_run=False,
        )
    except SkillAlreadyExistsError as exc:
        print(f"error: {exc}. Re-run with --force to replace it.", file=sys.stderr)
        return 1
    except AutoSkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    action = "replaced" if result["would_overwrite"] else "installed"
    print(f"{action}: {result['slug']} -> {result['dest_file']}")
    return 0


def _load_settings(settings_path: Path) -> dict[str, Any]:
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AutoSkillError(f"{settings_path} exists but is not valid JSON ({exc}); fix it by hand first.") from exc


def _save_settings(settings_path: Path, settings: dict[str, Any]) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _is_our_hook_entry(entry: dict[str, Any]) -> bool:
    """True if a UserPromptSubmit hook entry's command targets skill_suggest.py
    (any path -- lets us find and replace a stale/relocated copy)."""
    for h in entry.get("hooks", []):
        args = h.get("args") or []
        if any(str(a).endswith("skill_suggest.py") for a in args):
            return True
        if "skill_suggest.py" in str(h.get("command", "")):
            return True
    return False


def _find_hook_entry(settings: dict[str, Any]) -> dict[str, Any] | None:
    for entry in settings.get("hooks", {}).get("UserPromptSubmit", []):
        if _is_our_hook_entry(entry):
            return entry
    return None


def _default_codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


# Codex's UserPromptSubmit hook takes the same stdin contract as Claude
# Code's (a JSON payload with a "prompt" key) and explicitly accepts plain
# stdout as injected context, so hooks/skill_suggest.py runs unmodified --
# only the registration format differs (TOML config.toml vs JSON
# settings.json). config.toml has no stdlib writer, and this repo has no TOML
# dependency, so the managed block is inserted/removed by text markers
# rather than parsed and re-serialized, to avoid disturbing unrelated Codex
# config a full round-trip parse could reformat or drop.
_CODEX_HOOK_BEGIN = "# BEGIN auto-skill hook (managed by `auto-skill enable-hook --target codex`; do not edit by hand)"
_CODEX_HOOK_END = "# END auto-skill hook"
_CODEX_COMMAND_RE = re.compile(r"command\s*=\s*'python\s+\"([^\"]+)\"'")


def _codex_hook_block(hook_script_path: Path) -> str:
    return (
        f"{_CODEX_HOOK_BEGIN}\n"
        "[[hooks.UserPromptSubmit]]\n\n"
        "[[hooks.UserPromptSubmit.hooks]]\n"
        'type = "command"\n'
        f"command = 'python \"{hook_script_path}\"'\n"
        "timeout = 15\n"
        'statusMessage = "Routing prompt through auto-skill..."\n'
        f"{_CODEX_HOOK_END}\n"
    )


def _find_codex_hook_block(text: str) -> tuple[int, int] | None:
    """Return (start, end) character offsets of the managed block -- end is
    exclusive and absorbs one trailing newline -- or None if absent."""
    start = text.find(_CODEX_HOOK_BEGIN)
    if start == -1:
        return None
    end_marker = text.find(_CODEX_HOOK_END, start)
    if end_marker == -1:
        return None
    end = end_marker + len(_CODEX_HOOK_END)
    if end < len(text) and text[end] == "\n":
        end += 1
    return start, end


def _codex_registered_path(text: str) -> Path | None:
    bounds = _find_codex_hook_block(text)
    if bounds is None:
        return None
    match = _CODEX_COMMAND_RE.search(text[bounds[0] : bounds[1]])
    return Path(match.group(1)) if match else None


def _command_enable_hook_codex(args: argparse.Namespace) -> int:
    config_path = Path(args.settings_path) if args.settings_path else _default_codex_config_path()
    if not HOOK_SCRIPT_PATH.exists():
        print(f"error: hook script not found at {HOOK_SCRIPT_PATH}", file=sys.stderr)
        return 1

    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    existing_path = _codex_registered_path(text)
    if existing_path is not None:
        if existing_path == HOOK_SCRIPT_PATH:
            print(f"already enabled: {config_path} points at {HOOK_SCRIPT_PATH}")
            return 0
        print(f"a hook entry already exists pointing at {existing_path}, will replace it with {HOOK_SCRIPT_PATH}")

    print(
        "Privacy note: Auto Mode is optional. It locally skips non-task prompts, then sends each "
        "eligible task to the "
        f"configured search backend ({get_autoskill_url() or 'disabled'}) to look "
        "up a matching skill. The hosted backend processes task text transiently and does not "
        "retain it. Diagnostics are off by default and never include prompt text. See SECURITY.md."
    )
    if not args.yes:
        if not sys.stdin.isatty():
            print("error: refusing to enable non-interactively without --yes", file=sys.stderr)
            return 1
        answer = input("Enable the auto-skill routing hook? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("cancelled")
            return 1

    block = _codex_hook_block(HOOK_SCRIPT_PATH)
    bounds = _find_codex_hook_block(text)
    if bounds is not None:
        new_text = text[: bounds[0]] + block + text[bounds[1] :]
    else:
        prefix = text if not text or text.endswith("\n") else text + "\n"
        new_text = (prefix + "\n" if prefix else "") + block
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new_text, encoding="utf-8")
    print(f"enabled: wrote hook entry to {config_path}")
    print("Restart Codex CLI (or run /hooks once) for the change to take effect.")
    return 0


def _command_disable_hook_codex(args: argparse.Namespace) -> int:
    config_path = Path(args.settings_path) if args.settings_path else _default_codex_config_path()
    if not config_path.exists():
        print(f"not enabled: no auto-skill hook entry found in {config_path}")
        return 0

    text = config_path.read_text(encoding="utf-8")
    bounds = _find_codex_hook_block(text)
    if bounds is None:
        print(f"not enabled: no auto-skill hook entry found in {config_path}")
        return 0

    new_text = text[: bounds[0]] + text[bounds[1] :]
    config_path.write_text(new_text, encoding="utf-8")
    print(f"disabled: removed hook entry from {config_path}")
    return 0


def _command_enable_hook(args: argparse.Namespace) -> int:
    if getattr(args, "target", "claude") == "codex":
        return _command_enable_hook_codex(args)
    settings_path = Path(args.settings_path) if args.settings_path else _default_settings_path()
    if not HOOK_SCRIPT_PATH.exists():
        print(f"error: hook script not found at {HOOK_SCRIPT_PATH}", file=sys.stderr)
        return 1

    try:
        settings = _load_settings(settings_path)
    except AutoSkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    existing = _find_hook_entry(settings)
    if existing is not None:
        existing_args = (existing.get("hooks") or [{}])[0].get("args") or []
        if existing_args and str(existing_args[0]) == str(HOOK_SCRIPT_PATH):
            print(f"already enabled: {settings_path} points at {HOOK_SCRIPT_PATH}")
            return 0
        print(f"a hook entry already exists pointing at {existing_args}, will replace it with {HOOK_SCRIPT_PATH}")

    print(
        "Privacy note: Auto Mode is optional. It locally skips non-task prompts, then sends each "
        "eligible task to the "
        f"configured search backend ({get_autoskill_url() or 'disabled'}) to look "
        "up a matching skill. The hosted backend processes task text transiently and does not "
        "retain it. Diagnostics are off by default and never include prompt text. See SECURITY.md."
    )
    if not args.yes:
        if not sys.stdin.isatty():
            print("error: refusing to enable non-interactively without --yes", file=sys.stderr)
            return 1
        answer = input("Enable the auto-skill routing hook? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("cancelled")
            return 1

    settings.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    entries = settings["hooks"]["UserPromptSubmit"]
    entries[:] = [e for e in entries if not _is_our_hook_entry(e)]
    entries.append({
        "hooks": [{
            "type": "command",
            "command": "python",
            "args": [str(HOOK_SCRIPT_PATH)],
            "timeout": 15,
            "statusMessage": "Routing prompt through auto-skill...",
        }]
    })
    _save_settings(settings_path, settings)
    print(f"enabled: wrote hook entry to {settings_path}")
    print("Restart Claude Code (or open /hooks once) for the change to take effect.")
    return 0


def _command_disable_hook(args: argparse.Namespace) -> int:
    if getattr(args, "target", "claude") == "codex":
        return _command_disable_hook_codex(args)
    settings_path = Path(args.settings_path) if args.settings_path else _default_settings_path()
    try:
        settings = _load_settings(settings_path)
    except AutoSkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    entries = settings.get("hooks", {}).get("UserPromptSubmit", [])
    remaining = [e for e in entries if not _is_our_hook_entry(e)]
    if len(remaining) == len(entries):
        print(f"not enabled: no auto-skill hook entry found in {settings_path}")
        return 0

    settings["hooks"]["UserPromptSubmit"] = remaining
    if not remaining:
        del settings["hooks"]["UserPromptSubmit"]
    if not settings.get("hooks"):
        settings.pop("hooks", None)
    _save_settings(settings_path, settings)
    print(f"disabled: removed hook entry from {settings_path}")
    return 0


class _LoginCallbackHandler(BaseHTTPRequestHandler):
    """One-shot local HTTP handler for the OAuth loopback redirect. The
    backend's /auth/{provider}/callback redirects the browser here with the
    minted CLI token once login completes -- see backend/accounts_api.py."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's naming)
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        token = (parse_qs(parsed.query).get("token") or [None])[0]
        self.server.received_token = token  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = (
            "Login complete -- you can close this tab and return to your terminal."
            if token
            else "Login failed -- no token received. Return to your terminal and try again."
        )
        self.wfile.write(f"<html><body><p>{message}</p></body></html>".encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # keep the CLI's own output clean; failures still surface via _command_login


async def _command_login(args: argparse.Namespace) -> int:
    provider = args.provider
    if not provider:
        answer = input("Log in with [1] Google or [2] GitHub? ").strip()
        provider = "github" if answer == "2" else "google"

    server = HTTPServer(("127.0.0.1", 0), _LoginCallbackHandler)
    server.received_token = None  # type: ignore[attr-defined]
    server.timeout = 120  # bound handle_request() so an abandoned login doesn't hang forever
    port = server.server_address[1]

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    start_url = f"{get_autoskill_url()}/auth/{provider}/start?port={port}"
    print(f"Opening your browser to log in with {provider}...")
    print(f"If it doesn't open automatically, visit: {start_url}")
    webbrowser.open(start_url)

    thread.join(timeout=125)
    server.server_close()

    token = server.received_token  # type: ignore[attr-defined]
    if not token:
        print("error: login timed out or failed", file=sys.stderr)
        return 1

    save_credentials({"token": token})
    profile = await whoami()
    print(f"Logged in as {profile['email']}" if profile else "Logged in.")
    return 0


async def _command_logout(args: argparse.Namespace) -> int:
    await logout_backend()
    clear_credentials()
    print("logged out")
    return 0


async def _command_whoami(args: argparse.Namespace) -> int:
    profile = await whoami()
    if not profile:
        print("not logged in")
        return 1
    print(f"email: {profile['email']}")
    if profile.get("name"):
        print(f"name: {profile['name']}")
    return 0


async def _command_favorite(args: argparse.Namespace) -> int:
    try:
        await add_favorite(args.skill_id)
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"favorited: {args.skill_id}")
    return 0


async def _command_unfavorite(args: argparse.Namespace) -> int:
    try:
        removed = await remove_favorite(args.skill_id)
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"unfavorited: {args.skill_id}" if removed else f"not favorited: {args.skill_id}")
    return 0 if removed else 1


async def _command_favorites(args: argparse.Namespace) -> int:
    try:
        favorites = await list_favorites()
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not favorites:
        print("no favorites yet")
        return 0
    for index, skill in enumerate(favorites, start=1):
        print(f"{index}. {skill.get('name')}")
        if skill.get("url"):
            print(f"   {skill['url']}")
    return 0


async def _command_my_skills_add(args: argparse.Namespace) -> int:
    joined = " ".join(args.content)
    source = Path(joined)
    content = source.read_text(encoding="utf-8") if len(args.content) == 1 and source.exists() else joined
    try:
        skill = await submit_private_skill(args.name, args.description, content)
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"added private skill: {skill['name']} ({skill['id']})")
    return 0


async def _command_my_skills_list(args: argparse.Namespace) -> int:
    try:
        skills = await list_private_skills()
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not skills:
        print("no private skills yet")
        return 0
    for index, skill in enumerate(skills, start=1):
        print(f"{index}. {skill['name']} ({skill['id']})")
        if skill.get("description"):
            print(f"   {skill['description']}")
    return 0


async def _command_my_skills_remove(args: argparse.Namespace) -> int:
    try:
        removed = await remove_private_skill(args.skill_id)
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"removed: {args.skill_id}" if removed else f"not found: {args.skill_id}")
    return 0 if removed else 1


async def _command_mine_list_sessions(args: argparse.Namespace) -> int:
    sessions = list_local_sessions(args.client, since_days=args.since_days)
    if args.json:
        print(json.dumps(sessions, indent=2))
        return 0
    if not sessions:
        print("no local sessions found")
        return 0
    for index, session in enumerate(sessions, start=1):
        print(f"{index}. [{session['client']}] {session['session_id']}")
        if session.get("project"):
            print(f"   project: {session['project']}")
        print(f"   path: {session['path']}")
        print(f"   modified: {session['mtime_iso']}")
    return 0


async def _command_mine_save(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"error: {path} is not a file", file=sys.stderr)
        return 1
    try:
        entry = save_mined_skill(
            path.read_text(encoding="utf-8"),
            source_session_id=args.source_session,
            force=args.force,
        )
    except AutoSkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_warning_lines(entry.get("warnings") or [])
    print(f"saved: {entry['slug']} ({entry['name']})")
    print(f"  {get_mined_skills_dir() / (entry['slug'] + '.md')}")
    print("  private -- run `auto-skill mine publish` to share it, or `auto-skill install` to use it here")
    return 0


async def _command_mine_list(args: argparse.Namespace) -> int:
    entries = list_mined_skills()
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("no mined skills yet -- see the session-miner skill or `auto-skill mine list-sessions`")
        return 0
    for index, entry in enumerate(entries, start=1):
        status = "published" if entry.get("published") else "draft"
        print(f"{index}. {entry['slug']} ({status})")
        if entry.get("description"):
            print(f"   {entry['description']}")
    return 0


def _confirm_publish(slug: str) -> bool:
    if not sys.stdin.isatty():
        print("error: refusing non-interactive publish without --yes", file=sys.stderr)
        return False
    answer = input(f"Publish mined skill {slug!r} to your private catalog? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


async def _command_mine_publish(args: argparse.Namespace) -> int:
    entry = get_mined_skill(args.slug)
    if entry is None:
        print(f"error: no mined skill named {args.slug!r}", file=sys.stderr)
        return 1
    if not args.yes and not _confirm_publish(args.slug):
        print("cancelled")
        return 1
    try:
        published = await publish_mined_skill(args.slug)
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except AutoSkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"published: {published['slug']} -> private skill {published['published_skill_id']}")
    return 0


async def _command_mine_remove(args: argparse.Namespace) -> int:
    removed = remove_mined_skill(args.slug)
    print(f"removed: {args.slug}" if removed else f"not found: {args.slug}")
    return 0 if removed else 1


async def _command_weights_show(args: argparse.Namespace) -> int:
    summary = weights_summary()
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    if not summary["arms"]:
        print("no learned weights yet -- personalization builds up from `auto-skill feedback`")
        return 0
    for arm in summary["arms"]:
        state = "learned" if arm["learned"] else "warming up"
        print(f"{arm['key']}: weight={arm['estimated_weight']} observations={arm['observations']} ({state})")
    return 0


async def _command_weights_reset(args: argparse.Namespace) -> int:
    reset_weights()
    print("weights reset")
    return 0


async def _command_doctor(args: argparse.Namespace) -> int:
    ok = True
    settings_path = Path(args.settings_path) if args.settings_path else _default_settings_path()
    print("auto-skill doctor")
    print(f"python: {sys.version.split()[0]} ({sys.executable})")
    print(f"python resolvable on PATH: {'yes (' + shutil.which('python') + ')' if shutil.which('python') else 'no -- the hook calls `python`, so it must be on PATH'}")
    if not shutil.which("python"):
        ok = False

    print(f"claude skills home: {get_skills_home('claude')}")

    url = get_autoskill_url()
    print(f"self-hosted search: {url or 'disabled'}")
    if url:
        start = time.monotonic()
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{url.rstrip('/')}/healthz", timeout=10)
            elapsed = time.monotonic() - start
            if r.status_code == 200:
                print(f"  reachable: yes ({elapsed:.2f}s)")
            else:
                print(f"  reachable: no (HTTP {r.status_code})")
                ok = False
        except Exception as exc:
            print(f"  reachable: no ({exc})")
            ok = False
    else:
        print("  reachable: n/a (search disabled)")

    print(f"hook script: {HOOK_SCRIPT_PATH} ({'exists' if HOOK_SCRIPT_PATH.exists() else 'MISSING'})")
    if not HOOK_SCRIPT_PATH.exists():
        ok = False

    try:
        settings = _load_settings(settings_path)
        entry = _find_hook_entry(settings)
    except AutoSkillError as exc:
        print(f"hook registration: unreadable ({exc})")
        entry = None
        ok = False
    if entry is not None:
        registered_args = (entry.get("hooks") or [{}])[0].get("args") or []
        registered_path = Path(registered_args[0]) if registered_args else None
        if registered_path and registered_path.exists():
            match = " (matches this install)" if registered_path == HOOK_SCRIPT_PATH else " (DIFFERENT path than this install -- run enable-hook to repoint it)"
            print(f"hook registration: enabled in {settings_path} -> {registered_path}{match}")
        else:
            print(f"hook registration: enabled in {settings_path}, but {registered_path} does not exist on disk")
            ok = False
    else:
        print(f"hook registration: not enabled (run `auto-skill enable-hook` to turn it on)")

    try:
        import mcp  # noqa: F401

        print("mcp: installed")
    except Exception as exc:
        print(f"mcp: unavailable ({exc})")
        ok = False
    print("skill install targets:")
    for target in ("claude", "codex", "cursor", "copilot"):
        print(f"  {target}: {get_skills_home(target)}")

    profile = await whoami()
    print(f"account: logged in as {profile['email']}" if profile else "account: not logged in (run `auto-skill login`)")

    mined = list_mined_skills()
    drafts = sum(1 for entry in mined if not entry.get("published"))
    print(f"mined skills: {len(mined)} ({drafts} unpublished draft{'s' if drafts != 1 else ''})")

    weights = weights_summary()
    learned = sum(1 for arm in weights["arms"] if arm["learned"])
    print(f"personalization weights: {len(weights['arms'])} tracked, {learned} learned ({weights['path']})")

    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-skill",
        description="Find, preview, and safely install reusable AI agent skills.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="Search for skills matching a task.")
    search.add_argument("task", nargs="+", help="Task description to search for.")
    search.set_defaults(func=_command_search)

    route = subparsers.add_parser("route", help="Route a task to one best reusable skill.")
    route.add_argument("task", nargs="+", help="Task description to route.")
    route.add_argument("--json", action="store_true", help="Print the full routing payload as JSON.")
    route.add_argument("--show-content", action="store_true", help="Print selected skill content.")
    route.add_argument("--max-chars", type=int, default=12000, help="Maximum skill content characters to print.")
    route.set_defaults(func=_command_route)

    route_prompt = subparsers.add_parser("route-prompt", help="Preflight a raw user prompt and emit injectable context.")
    route_prompt.add_argument("prompt", nargs="+", help="Raw user prompt to route if it is skill-shaped.")
    route_prompt.add_argument("--json", action="store_true", help="Print the full prompt-routing payload as JSON.")
    route_prompt.add_argument("--context-only", action="store_true", help="Print only the context to inject.")
    route_prompt.set_defaults(func=_command_route_prompt)

    feedback = subparsers.add_parser("feedback", help="Record privacy-safe route outcome feedback.")
    feedback.add_argument("route_id", help="Route id returned by route or route-prompt JSON.")
    feedback.add_argument("outcome", choices=["used", "skipped", "installed", "failed", "dismissed"], help="Outcome to record.")
    feedback.add_argument("--note", default="", help="Deprecated compatibility option; ignored and never retained.")
    feedback.set_defaults(func=_command_feedback)

    metrics = subparsers.add_parser("metrics", help="Show local route analytics and launch budget breaches.")
    metrics.add_argument("--hours", type=int, default=24, help="Recent route-event window to inspect.")
    metrics.add_argument("--base-url", default="", help="Override AUTOSKILL_URL; use a local/loopback API.")
    metrics.add_argument("--json", action="store_true", help="Print raw /route-metrics JSON.")
    metrics.set_defaults(func=_command_metrics)

    survey = subparsers.add_parser(
        "survey", help="Answer the voluntary 'was Auto-Skill useful?' prompt (requires login)."
    )
    survey.add_argument("response", choices=["helpful", "not_useful", "skip"], help="Your answer.")
    survey.set_defaults(func=_command_survey)

    impact_report = subparsers.add_parser(
        "impact-report", help="Show your personal Auto-Skill impact report (requires login)."
    )
    impact_report.add_argument("--days", type=int, default=30, help="Window size in days (default 30).")
    impact_report.add_argument("--json", action="store_true", help="Print raw /impact-report JSON.")
    impact_report.set_defaults(func=_command_impact_report)

    measurement_mode = subparsers.add_parser(
        "measurement-mode", help="Manage opt-in Measurement Mode (requires login)."
    )
    measurement_mode_sub = measurement_mode.add_subparsers(dest="measurement_mode_command", required=True)

    measurement_mode_status = measurement_mode_sub.add_parser("status", help="Show current Measurement Mode settings.")
    measurement_mode_status.set_defaults(func=_command_measurement_mode_status)

    measurement_mode_enable = measurement_mode_sub.add_parser(
        "enable", help="Opt in: randomly withhold a small share of full-tier routes to measure lift."
    )
    measurement_mode_enable.add_argument(
        "--holdout-rate", type=float, default=None, help="Fraction of eligible routes to hold out (default 0.05)."
    )
    measurement_mode_enable.set_defaults(func=_command_measurement_mode_enable)

    measurement_mode_disable = measurement_mode_sub.add_parser("disable", help="Opt out at any time.")
    measurement_mode_disable.set_defaults(func=_command_measurement_mode_disable)

    preview = subparsers.add_parser("preview", help="Preview a skill by URL or task description.")
    preview.add_argument("source", nargs="+", help="Skill URL or task description.")
    preview.add_argument("--max-chars", type=int, default=12000, help="Maximum skill content characters to print.")
    preview.set_defaults(func=_command_preview)

    install = subparsers.add_parser("install", help="Install a skill by URL or task description.")
    install.add_argument("source", nargs="+", help="Skill URL or task description.")
    install.add_argument(
        "--target",
        choices=["claude", "codex", "cursor", "copilot"],
        default="claude",
        help="Local Agent Skills target.",
    )
    install.add_argument("--name", default="", help="Override installed skill name.")
    install.add_argument("--yes", action="store_true", help="Approve install without an interactive prompt.")
    install.add_argument("--force", action="store_true", help="Overwrite an existing skill with the same slug.")
    install.add_argument("--dry-run", action="store_true", help="Show what would happen without writing files.")
    install.set_defaults(func=_command_install)

    validate = subparsers.add_parser("validate", help="Validate a local SKILL.md before installing or sharing it.")
    validate.add_argument("path", help="Path to a SKILL.md file.")
    validate.add_argument("--json", action="store_true", help="Print the structured validation result as JSON.")
    validate.set_defaults(func=_command_validate)

    doctor = subparsers.add_parser("doctor", help="Check local auto-skill setup.")
    doctor.add_argument("--settings-path", default="", help="Override the Claude Code settings.json path (for testing).")
    doctor.set_defaults(func=_command_doctor)

    enable_hook = subparsers.add_parser("enable-hook", help="Register the auto-skill routing hook in Claude Code or Codex CLI.")
    enable_hook.add_argument("--target", choices=["claude", "codex"], default="claude", help="Which client to register the hook for.")
    enable_hook.add_argument("--yes", action="store_true", help="Approve without an interactive prompt.")
    enable_hook.add_argument("--settings-path", default="", help="Override the settings.json/config.toml path (for testing).")
    enable_hook.set_defaults(func=_command_enable_hook)

    disable_hook = subparsers.add_parser("disable-hook", help="Remove the auto-skill routing hook from Claude Code or Codex CLI.")
    disable_hook.add_argument("--target", choices=["claude", "codex"], default="claude", help="Which client to remove the hook from.")
    disable_hook.add_argument("--settings-path", default="", help="Override the settings.json/config.toml path (for testing).")
    disable_hook.set_defaults(func=_command_disable_hook)

    login = subparsers.add_parser("login", help="Log in with Google or GitHub to get your own auto-skill account.")
    login.add_argument("--provider", choices=["google", "github"], default="", help="Skip the interactive prompt.")
    login.set_defaults(func=_command_login)

    logout = subparsers.add_parser("logout", help="Log out and forget the stored session.")
    logout.set_defaults(func=_command_logout)

    whoami_cmd = subparsers.add_parser("whoami", help="Show the logged-in account, if any.")
    whoami_cmd.set_defaults(func=_command_whoami)

    favorite = subparsers.add_parser("favorite", help="Favorite a skill by id (requires login).")
    favorite.add_argument("skill_id", help="Skill id to favorite.")
    favorite.set_defaults(func=_command_favorite)

    unfavorite = subparsers.add_parser("unfavorite", help="Remove a favorited skill by id (requires login).")
    unfavorite.add_argument("skill_id", help="Skill id to unfavorite.")
    unfavorite.set_defaults(func=_command_unfavorite)

    favorites = subparsers.add_parser("favorites", help="List your favorited skills (requires login).")
    favorites.set_defaults(func=_command_favorites)

    my_skills = subparsers.add_parser("my-skills", help="Manage your private skill submissions (requires login).")
    my_skills_sub = my_skills.add_subparsers(dest="my_skills_command", required=True)

    my_skills_add = my_skills_sub.add_parser("add", help="Submit a private skill.")
    my_skills_add.add_argument("name", help="Skill name.")
    my_skills_add.add_argument("content", nargs="+", help="Skill content, or a path to a SKILL.md file.")
    my_skills_add.add_argument("--description", default="", help="Short description.")
    my_skills_add.set_defaults(func=_command_my_skills_add)

    my_skills_list = my_skills_sub.add_parser("list", help="List your private skills.")
    my_skills_list.set_defaults(func=_command_my_skills_list)

    my_skills_remove = my_skills_sub.add_parser("remove", help="Remove a private skill by id.")
    my_skills_remove.add_argument("skill_id", help="Private skill id to remove.")
    my_skills_remove.set_defaults(func=_command_my_skills_remove)

    mine = subparsers.add_parser("mine", help="Mine local AI session transcripts into private candidate skills.")
    mine_sub = mine.add_subparsers(dest="mine_command", required=True)

    mine_list_sessions = mine_sub.add_parser("list-sessions", help="List local session transcripts (metadata only).")
    mine_list_sessions.add_argument("--client", choices=["claude", "codex", "all"], default="all", help="Which client's sessions to list.")
    mine_list_sessions.add_argument("--since-days", type=float, default=None, help="Only sessions modified in the last N days.")
    mine_list_sessions.add_argument("--json", action="store_true", help="Print session metadata as JSON.")
    mine_list_sessions.set_defaults(func=_command_mine_list_sessions)

    mine_save = mine_sub.add_parser("save", help="Validate and privately store a drafted SKILL.md mined from a session.")
    mine_save.add_argument("path", help="Path to the drafted SKILL.md.")
    mine_save.add_argument("--source-session", default="", help="Session id this draft was mined from.")
    mine_save.add_argument("--force", action="store_true", help="Save even if it looks like a duplicate of an already-mined skill.")
    mine_save.set_defaults(func=_command_mine_save)

    mine_list = mine_sub.add_parser("list", help="List locally mined skills.")
    mine_list.add_argument("--json", action="store_true", help="Print mined-skill metadata as JSON.")
    mine_list.set_defaults(func=_command_mine_list)

    mine_publish = mine_sub.add_parser("publish", help="Publish a locally mined skill to your private catalog (requires login).")
    mine_publish.add_argument("slug", help="Mined skill slug (see `auto-skill mine list`).")
    mine_publish.add_argument("--yes", action="store_true", help="Approve publish without an interactive prompt.")
    mine_publish.set_defaults(func=_command_mine_publish)

    mine_remove = mine_sub.add_parser("remove", help="Remove a locally mined skill.")
    mine_remove.add_argument("slug", help="Mined skill slug to remove.")
    mine_remove.set_defaults(func=_command_mine_remove)

    weights = subparsers.add_parser("weights", help="Inspect or reset local personalization weights.")
    weights_sub = weights.add_subparsers(dest="weights_command", required=True)

    weights_show = weights_sub.add_parser("show", help="Show learned per-skill/tag weights on this machine.")
    weights_show.add_argument("--json", action="store_true", help="Print weights as JSON.")
    weights_show.set_defaults(func=_command_weights_show)

    weights_reset = weights_sub.add_parser("reset", help="Clear all learned local weights.")
    weights_reset.set_defaults(func=_command_weights_reset)

    return parser


async def _main_async(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    if asyncio.iscoroutine(result):
        return await result
    return int(result)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(_main_async(argv))


if __name__ == "__main__":
    raise SystemExit(main())
