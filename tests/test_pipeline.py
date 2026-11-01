"""End-to-end: brief -> mesh -> validation -> preview -> viewer -> handoff."""

import json
from pathlib import Path

from forge3d import context, handoff, meshio, render, repair, server, viewer
from forge3d.spec import ModelSpec

CHAIR = Path(__file__).resolve().parents[1] / "examples" / "procedural_chair.py"


def test_create_model_full_pipeline():
    result = server.create_model(
        prompt="side chair",
        name="chair",
        purpose="game",
        backend="procedural",
        source=CHAIR.read_text(),
        dimensions={"x": 450, "y": 480, "z": 850},
        scale_to_spec=True,
    )
    assert result["ok"], result
    assert Path(result["path"]).exists()
    assert Path(result["viewer"]).exists()
    assert result["validation"]["ok"] is True
    assert result["card"]["triangles"] > 0
    # the return payload must stay small enough to be usable in context
    assert len(json.dumps(result)) < 20_000


def test_create_model_bad_source_returns_hint():
    result = server.create_model(name="broken", backend="procedural", source="def build(: pass")
    assert result["ok"] is False
    assert result["hint"]


def test_plan_model_flags_missing_fields():
    plan = server.plan_model(prompt="a robot")
    assert plan["ok"] is True
    assert plan["missing"]
    assert plan["next"].startswith("ask the user")


def test_plan_model_complete_spec_is_ready():
    plan = server.plan_model(
        prompt="a bracket", purpose="print", dimensions={"x": 120, "y": 60, "z": 40}
    )
    assert plan["missing"] == []
    assert plan["next"] == "call create_model"


def test_review_and_repair_roundtrip(tmp_model):
    review = server.review_model(str(tmp_model), spec={"purpose": "print", "units": "mm"})
    assert "verdict" in review

    fixed = server.repair_model(str(tmp_model))
    assert fixed["ok"] is True
    assert fixed["actions"]
    assert Path(fixed["path"]).exists()


def test_convert_and_inspect(tmp_model, tmp_path):
    stl = server.convert_model(str(tmp_model), fmt="stl")
    assert stl["ok"] and stl["path"].endswith(".stl")

    inspected = server.inspect_model(stl["path"])
    assert inspected["stats"]["faces"] > 0


def test_validate_model_tool(tmp_model):
    out = server.validate_model(str(tmp_model), profile="print")
    assert out["ok"] is True
    assert "verdict" in out


def test_missing_file_returns_hint_not_exception():
    out = server.inspect_model("/nope/does-not-exist.glb")
    assert out["ok"] is False
    assert out["hint"]


def test_model_card_stays_small(tmp_model):
    card = server.model_card(str(tmp_model))["card"]
    assert context.estimate_tokens(json.dumps(card)) < context.CARD_BUDGET_TOKENS
    assert card["triangles"] > 0


def test_viewer_embeds_model(tmp_model, tmp_path):
    html = viewer.build_viewer(tmp_model, tmp_path / "v.html", title="Cube Test")
    text = html.read_text(encoding="utf-8")
    assert "data:model/gltf-binary;base64," in text
    assert "Cube Test" in text
    assert "three" in text


def test_viewer_converts_non_glb(cube, tmp_path):
    stl = tmp_path / "cube.stl"
    cube.export(str(stl))
    html = viewer.build_viewer(stl, tmp_path / "from-stl.html")
    assert html.exists()
    assert (tmp_path / "cube.glb").exists()


def test_render_produces_previews(tmp_model):
    result = render.render_file(tmp_model, Path(tmp_model).parent / "previews")
    if not render.available():
        assert result["ok"] is False
        return
    assert result["ok"] is True
    for path in result["previews"].values():
        assert Path(path).exists()
        assert Path(path).stat().st_size > 1000


def test_repair_fixes_an_open_surface(open_surface, tmp_path):
    mesh, actions = repair.repair(open_surface)
    assert actions
    assert len(mesh.faces) > 0


def test_handoff_writes_json_and_markdown(tmp_path):
    spec = ModelSpec(name="chair", purpose="game", dimensions={"x": 450, "y": 480, "z": 850})
    packet = handoff.build_handoff(spec, {"model": "chair.glb"}, backend="procedural")
    js = packet.save(tmp_path / "h.json")
    md = tmp_path / "h.md"
    md.write_text(packet.to_markdown(), encoding="utf-8")

    assert json.loads(js.read_text())["spec"]["name"] == "chair"
    text = md.read_text(encoding="utf-8")
    assert "# Handoff" in text and "chair" in text


def test_handoff_load_roundtrip(tmp_path):
    spec = ModelSpec(name="x")
    packet = handoff.build_handoff(spec, {})
    p = packet.save(tmp_path / "p.json")
    again = handoff.Handoff.load(p)
    assert again.id == packet.id


def test_checklists_are_complete_and_tickable():
    for name in handoff.CHECKLISTS:
        items = handoff.new_checklist(name)
        assert items
        assert all(v is False for v in items.values())
        progress = handoff.checklist_progress(dict.fromkeys(items, True))
        assert progress["percent"] == 100.0


def test_unknown_checklist_reports_options():
    out = server.checklist("nonexistent")
    assert out["ok"] is False
    assert "choose from" in out["hint"]


def test_jobs_are_recorded():
    server.create_model(name="jobtest", backend="procedural",
                        source="import trimesh\ndef build(): return trimesh.creation.box()")
    jobs = server.list_jobs()
    assert jobs["count"] >= 1
    assert jobs["jobs"][0]["status"] in {"succeeded", "failed"}


def test_meshio_stats_never_raises_on_empty():
    import trimesh

    st = meshio.stats(trimesh.Trimesh())
    assert st["faces"] == 0
