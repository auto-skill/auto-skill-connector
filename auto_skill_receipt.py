"""Homepage-like ordered-plan receipt for MCP, CLI, and Auto Mode hooks.

Stdlib-only so hooks/skill_suggest.py can import it without httpx/mcp.
Formats the 2-slot card (policy + primary) with truthful verification chips
based on actual route gates — never invents a third "verify" skill or claims
malware-scan wording when only risk heuristics are present.
"""

from __future__ import annotations

import json
from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _skill_name(skill: dict[str, Any]) -> str:
    return str(skill.get("name") or "unknown")


def _verification(skill: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(skill.get("verification"))


def ordered_plan_slots(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Return up to two (role, name) slots: policy then primary."""
    plan = _as_dict(payload.get("skill_plan"))
    slots: list[tuple[str, str]] = []

    for policy in plan.get("policy_skills") or []:
        if not isinstance(policy, dict):
            continue
        if not (policy.get("name") or policy.get("url") or policy.get("capsule")):
            continue
        slots.append(("policy", _skill_name(policy)))
        break  # 2-slot card: at most one policy line

    primary = _as_dict(plan.get("primary_skill"))
    if primary.get("name") or primary.get("url"):
        slots.append(("primary", _skill_name(primary)))
    else:
        selected = _as_dict(payload.get("selected_skill") or payload.get("skill"))
        if selected.get("name") or selected.get("url"):
            # Avoid duplicating the policy name when the only selection is the policy.
            if not slots or _skill_name(selected) != slots[0][1]:
                slots.append(("primary", _skill_name(selected)))

    return slots[:2]


def _gate_skills(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Skills whose verification/risk fields feed the truthful chip line."""
    plan = _as_dict(payload.get("skill_plan"))
    selected = _as_dict(payload.get("selected_skill") or payload.get("skill"))
    skills: list[dict[str, Any]] = []
    for policy in plan.get("policy_skills") or []:
        if isinstance(policy, dict) and (policy.get("name") or policy.get("capsule")):
            skills.append(policy)
            break
    primary = _as_dict(plan.get("primary_skill"))
    if primary.get("name") or primary.get("url"):
        merged = dict(primary)
        # Plan primary often omits gate fields that live on selected_skill.
        if selected:
            if not _verification(merged) and selected.get("verification"):
                merged["verification"] = selected.get("verification")
            if merged.get("risk_score") is None and selected.get("risk_score") is not None:
                merged["risk_score"] = selected.get("risk_score")
        skills.append(merged)
    elif selected.get("name") or selected.get("url"):
        skills.append(selected)
    return skills


def format_verified_chips(payload: dict[str, Any]) -> str:
    """Build truthful chips from actual route gates (empty if none apply)."""
    skills = _gate_skills(payload)
    if not skills:
        return ""

    chips: list[str] = []
    hash_ok = all(_verification(s).get("content_hash_verified") is True for s in skills)
    static_ok = all(_verification(s).get("static_instruction_only") is True for s in skills)
    risk_scores = [s.get("risk_score") for s in skills]
    risk_known = all(isinstance(score, (int, float)) for score in risk_scores)
    risk_zero = risk_known and all(int(score) == 0 for score in risk_scores)

    if hash_ok and static_ok:
        chips.append("✓ verified")
    if risk_zero:
        chips.append("risk_score=0")
    if hash_ok:
        chips.append("content-hash verified")
    if static_ok:
        chips.append("static guidance only")
    chips.append("no skill install")
    return " | ".join(chips)


def _route_tier(payload: dict[str, Any]) -> str:
    return str(
        payload.get("route_tier")
        or payload.get("route_type")
        or payload.get("tier")
        or ""
    ).lower()


def format_route_receipt(payload: dict[str, Any]) -> str:
    """Homepage-structured ordered-plan receipt (header + slots + chips).

    Returns "" when there is nothing useful to show (unrouted / empty plan).
    """
    if payload.get("routed") is False:
        return ""

    tier = _route_tier(payload)
    if tier in {"none", ""}:
        return ""

    slots = ordered_plan_slots(payload)
    if not slots:
        return ""

    # Hints keep candidate lists elsewhere; only show a light card when a plan exists.
    count = len(slots)
    count_label = "1 skill routed" if count == 1 else f"{count} skills routed"
    lines = ["AUTO-SKILL", count_label]
    for index, (role, name) in enumerate(slots, start=1):
        lines.append(f"{index:02d}  {role:<8}{name}")

    if tier not in {"hint"}:
        chips = format_verified_chips(payload)
        if chips:
            lines.append(chips)
    else:
        lines.append("hint only | not verified for injection | no skill install")

    return "\n".join(lines)


def format_route_card_markdown(payload: dict[str, Any]) -> str:
    """Rich markdown card for in-chat / MCP tool UIs (homepage-shaped, 2 slots).

    Returns "" when there is nothing useful to show (unrouted / empty plan).
    """
    if payload.get("routed") is False:
        return ""

    tier = _route_tier(payload)
    if tier in {"none", ""}:
        return ""

    slots = ordered_plan_slots(payload)
    if not slots:
        return ""

    count = len(slots)
    count_label = "1 skill routed" if count == 1 else f"{count} skills routed"
    lines = [
        "### AUTO-SKILL",
        f"**{count_label}**",
        "",
        "| # | role | skill |",
        "|---|------|-------|",
    ]
    for index, (role, name) in enumerate(slots, start=1):
        safe_name = str(name).replace("|", "\\|")
        lines.append(f"| {index:02d} | {role} | {safe_name} |")
    lines.append("")

    if tier in {"hint"}:
        lines.append(
            "`hint only` · `not verified for injection` · `no skill install`"
        )
    else:
        chips = format_verified_chips(payload)
        if chips:
            # Pipe chips → middot chips so markdown tables stay intact.
            pretty = " · ".join(
                f"`{part.strip()}`" for part in chips.split("|") if part.strip()
            )
            lines.append(pretty)

    return "\n".join(lines)


def format_mcp_route_tool_result(payload: dict[str, Any]) -> str:
    """Card-first markdown MCP tool output for Cursor / Claude agent sidebars.

    Visible UI is the markdown card; a compact JSON machine block follows so
    the model can still apply tier, content, and plan fields.
    """
    card = format_route_card_markdown(payload) or format_route_receipt(payload)
    if not card:
        summary = _as_dict(payload.get("route_summary"))
        message = str(
            summary.get("reason")
            or payload.get("message")
            or "No reusable skill was selected."
        )
        return (
            "### AUTO-SKILL\n"
            "**no route**\n\n"
            f"{message}\n\n"
            "Continue normally without injecting skill instructions."
        )

    tier = _route_tier(payload)
    apply_line = (
        "APPLY — treat the card above as user-visible routing UI. "
        "Follow the ordered plan (policy then primary). Do not invent a third skill."
        if tier not in {"hint"}
        else "HINT — suggestions only. Do not treat candidates as active instructions."
    )

    machine: dict[str, Any] = {
        "tier": tier or None,
        "route_type": payload.get("route_type"),
        "route_id": payload.get("route_id"),
        "route_receipt": payload.get("route_receipt") or format_route_receipt(payload),
        "route_card_markdown": card,
        "route_summary": payload.get("route_summary"),
        "skill_plan": payload.get("skill_plan"),
        "selected_skill": payload.get("selected_skill") or payload.get("skill"),
        "candidates": payload.get("candidates"),
        "skill_content": payload.get("skill_content") or payload.get("content"),
        "context_guard": payload.get("context_guard"),
        "content_url": payload.get("content_url"),
        "warnings": payload.get("warnings"),
    }
    # Drop empty noise so the sidebar stays scannable.
    compact = {key: value for key, value in machine.items() if value not in (None, "", [], {})}

    return (
        f"{card}\n\n"
        f"{apply_line}\n\n"
        "<!-- auto-skill machine fields; apply guidance from these -->\n"
        "```json\n"
        f"{json.dumps(compact, ensure_ascii=False, indent=2, default=str)}\n"
        "```"
    )
