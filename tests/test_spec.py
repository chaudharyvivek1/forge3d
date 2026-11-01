from forge3d.spec import ModelSpec


def test_slugifies_name():
    spec = ModelSpec(name="Side Chair!!", prompt="x")
    assert spec.name == "side-chair"


def test_rejects_unknown_purpose_and_units():
    spec = ModelSpec(purpose="furniture", units="furlongs")
    assert spec.purpose == "any"
    assert spec.units == "mm"


def test_extent_reads_dimensions():
    spec = ModelSpec(dimensions={"x": 180, "y": 90, "z": 75})
    assert spec.extent == [180.0, 90.0, 75.0]


def test_extent_none_when_absent():
    assert ModelSpec().extent is None


def test_missing_lists_the_gaps():
    gaps = ModelSpec(prompt="a chair").missing()
    assert "purpose (print/web/game/cad)" in gaps
    assert "dimensions (real-world size)" in gaps


def test_missing_empty_when_complete():
    spec = ModelSpec(prompt="a chair", purpose="print", dimensions={"x": 450, "y": 480, "z": 850})
    assert spec.missing() == []


def test_roundtrip_dict():
    spec = ModelSpec(name="bracket", purpose="print", dimensions={"x": 1, "y": 2, "z": 3})
    again = ModelSpec.from_dict(spec.to_dict())
    assert again.to_dict() == spec.to_dict()


def test_from_dict_ignores_unknown_keys():
    spec = ModelSpec.from_dict({"name": "x", "nonsense": True})
    assert spec.name == "x"


def test_prompt_for_backend_includes_numbers():
    spec = ModelSpec(prompt="teapot", style="matte", dimensions={"x": 100, "y": 80, "z": 120})
    text = spec.prompt_for_backend()
    assert "teapot" in text and "matte" in text and "100" in text


def test_summary_is_readable():
    spec = ModelSpec(name="chair", purpose="game", dimensions={"x": 450, "y": 480, "z": 850})
    assert "game" in spec.summary() and "450" in spec.summary()
