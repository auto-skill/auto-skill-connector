"""Command line interface for auto-skill."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from auto_skill_core import (
    AutoSkillError,
    SkillAlreadyExistsError,
    UnsupportedTargetError,
    _fetch_content,
    _search,
    get_autoskill_url,
    get_skills_home,
    install_skill_from_content,
    is_url,
    recommend_skill_payload,
    route_prompt_payload,
    route_task_payload,
)


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

    if not payload.get("routed"):
        print(payload.get("message", "No matching skill found."))
        return 1

    skill = payload.get("selected_skill") or {}
    print("selected skill:")
    _print_candidate(1, skill)
    print()
    print("next action: apply the returned skill_content in-turn")
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
    print("injectable context:")
    print(payload.get("context", ""))
    return 0


async def _resolve_cli_skill(source: str) -> dict[str, Any]:
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


def _confirm_install(dest_hint: Path) -> bool:
    if not sys.stdin.isatty():
        print("error: refusing non-interactive install without --yes", file=sys.stderr)
        return False
    answer = input(f"Install to {dest_hint}? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


async def _command_install(args: argparse.Namespace) -> int:
    source = " ".join(args.source).strip()
    if args.target == "codex":
        print(
            "Codex does not currently support permanent Claude SKILL.md installs. "
            "Use the auto-skill MCP recommend_skill tool and apply the returned instructions in-turn.",
            file=sys.stderr,
        )
        return 2

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

    if args.dry_run:
        print("dry run: no files written")
        return 0

    if not args.yes and not _confirm_install(preview_result["dest_file"]):
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


def _command_doctor(args: argparse.Namespace) -> int:
    del args
    print("auto-skill doctor")
    print(f"python: {sys.version.split()[0]}")
    print(f"self-hosted search: {get_autoskill_url() or 'disabled'}")
    try:
        print(f"claude skills home: {get_skills_home('claude')}")
    except UnsupportedTargetError as exc:
        print(f"claude skills home: unavailable ({exc})")
    try:
        import mcp  # noqa: F401

        print("mcp: installed")
    except Exception as exc:
        print(f"mcp: unavailable ({exc})")
        return 1
    print("codex permanent skill install: unsupported; use MCP recommend_skill in-turn")
    return 0


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

    preview = subparsers.add_parser("preview", help="Preview a skill by URL or task description.")
    preview.add_argument("source", nargs="+", help="Skill URL or task description.")
    preview.add_argument("--max-chars", type=int, default=12000, help="Maximum skill content characters to print.")
    preview.set_defaults(func=_command_preview)

    install = subparsers.add_parser("install", help="Install a skill by URL or task description.")
    install.add_argument("source", nargs="+", help="Skill URL or task description.")
    install.add_argument("--target", choices=["claude", "codex"], default="claude", help="Install target.")
    install.add_argument("--name", default="", help="Override installed skill name.")
    install.add_argument("--yes", action="store_true", help="Approve install without an interactive prompt.")
    install.add_argument("--force", action="store_true", help="Overwrite an existing skill with the same slug.")
    install.add_argument("--dry-run", action="store_true", help="Show what would happen without writing files.")
    install.set_defaults(func=_command_install)

    doctor = subparsers.add_parser("doctor", help="Check local auto-skill setup.")
    doctor.set_defaults(func=_command_doctor)
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
