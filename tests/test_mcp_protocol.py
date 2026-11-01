"""End-to-end tests through the real MCP protocol layer.

These matter more than the unit tests: `fastmcp`'s in-memory client calls tools
exactly as a remote agent would, including running synchronous tools in a
threadpool. That is the path where the v0.1.0 `signal.alarm` timeout died, and
no amount of direct function-calling would have caught it.
"""

import asyncio
import json

from fastmcp import Client

from forge3d.server import mcp

EXPECTED_TOOLS = {
    "checklist", "convert_model", "create_handoff", "create_model", "doctor",
    "get_job", "inspect_model", "list_backends", "list_jobs", "make_viewer",
    "create_planet", "create_shape", "list_providers", "list_shapes",
    "make_scene_viewer",
    "model_card", "plan_model", "render_model", "repair_model", "review_model",
    "validate_model",
}

CUBE = "import trimesh\ndef build(): return trimesh.creation.box(extents=[20, 20, 20])"


def _call(name: str, args: dict) -> dict:
    """Run one tool call through the MCP client and decode the JSON payload."""

    async def go():
        async with Client(mcp) as client:
            result = await client.call_tool(name, args)
            return json.loads(result.content[0].text)

    return asyncio.run(go())


def _list_tools() -> list[str]:
    async def go():
        async with Client(mcp) as client:
            return [t.name for t in await client.list_tools()]

    return asyncio.run(go())


def test_every_expected_tool_is_exposed():
    assert set(_list_tools()) == EXPECTED_TOOLS


def test_every_tool_has_a_description():
    """A tool an agent cannot understand is a tool it will misuse."""

    async def go():
        async with Client(mcp) as client:
            return [(t.name, t.description or "") for t in await client.list_tools()]

    for name, description in asyncio.run(go()):
        assert len(description) > 30, f"{name} has no useful description"


def test_doctor_over_mcp():
    out = _call("doctor", {})
    assert out["ok"] is True
    assert out["report"]["engines"]["procedural"]["available"] is True


def test_create_model_over_mcp_runs_agent_code_in_the_threadpool():
    """The regression that motivated this file."""
    out = _call("create_model", {
        "name": "mcp-e2e", "purpose": "game", "backend": "procedural",
        "source": CUBE, "render_previews": False,
    })
    assert out["ok"] is True, out
    assert out["card"]["triangles"] == 12
    assert out["validation"]["ok"] is True


def test_plan_model_over_mcp_flags_missing_dimensions():
    out = _call("plan_model", {"prompt": "a wall bracket", "purpose": "print"})
    assert out["missing"] == ["dimensions (real-world size)"]


def test_list_backends_over_mcp():
    out = _call("list_backends", {})
    names = {b["name"] for b in out["backends"]}
    assert names == {"procedural", "image", "openscad", "blender", "cadquery", "hosted"}


def test_bad_input_returns_a_hint_not_a_protocol_error():
    """A tool that raises crashes the turn; a tool that hints lets the agent retry."""
    out = _call("inspect_model", {"path": "/definitely/not/here.glb"})
    assert out["ok"] is False
    assert out["hint"]


def test_validate_model_over_mcp():
    created = _call("create_model", {
        "name": "mcp-validate", "backend": "procedural", "source": CUBE,
        "render_previews": False, "make_viewer": False,
    })
    out = _call("validate_model", {"path": created["path"], "profile": "print"})
    assert out["ok"] is True
    assert "PASS" in out["verdict"]


def test_model_card_over_mcp_stays_small():
    created = _call("create_model", {
        "name": "mcp-card", "backend": "procedural", "source": CUBE,
        "render_previews": False, "make_viewer": False,
    })
    out = _call("model_card", {"path": created["path"]})
    assert len(json.dumps(out)) < 2000


def test_checklist_over_mcp():
    out = _call("checklist", {"name": "print"})
    assert out["ok"] is True
    assert out["progress"]["total"] > 0


def test_job_history_over_mcp():
    _call("create_model", {"name": "mcp-job", "backend": "procedural", "source": CUBE,
                           "render_previews": False, "make_viewer": False})
    out = _call("list_jobs", {})
    assert out["count"] >= 1
    assert out["jobs"][0]["status"] in {"succeeded", "failed"}
