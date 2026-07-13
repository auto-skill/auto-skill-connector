from __future__ import annotations

import asyncio

import mcp_server


def _tools_by_name() -> dict:
    return {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}


def test_mcp_instructions_proactively_route_privacy_minimized_tasks() -> None:
    instructions = mcp_server.mcp.instructions
    assert "Proactively call route_task once" in instructions
    assert "Do not wait for the user to ask for a skill" in instructions
    assert "omit secrets, personal data, pasted content" in instructions
    assert "do not call repeatedly for the same task" in instructions
    assert "only when the user explicitly enabled" in instructions


def test_routing_tools_advertise_read_only_idempotent_semantics() -> None:
    tools = _tools_by_name()
    for name in ("route_prompt", "route_task", "recommend_skill"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is True
        assert annotations.destructiveHint is False
        assert annotations.idempotentHint is True
        assert annotations.openWorldHint is True

    feedback = tools["record_feedback"].annotations
    assert feedback is not None
    assert feedback.readOnlyHint is False
    assert feedback.destructiveHint is False


def test_route_task_and_raw_prompt_descriptions_preserve_consent_boundary() -> None:
    tools = _tools_by_name()
    assert "without waiting for the user" in (tools["route_task"].description or "")
    assert "Do not send raw prompts proactively" in (tools["route_prompt"].description or "")
