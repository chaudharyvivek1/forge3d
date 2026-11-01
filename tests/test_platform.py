"""Cross-platform behaviour: discovery, timeouts, encoding, filenames.

Every test here exists because the corresponding code path was broken on at
least one platform, or broke the moment it ran anywhere other than a Linux
main thread.
"""

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from forge3d import platform_support as ps
from forge3d.spec import ModelSpec


# --- timeouts -------------------------------------------------------------
def test_safe_timeout_returns_the_value():
    assert ps.safe_timeout(lambda: 42, 5) == 42


def test_safe_timeout_raises_on_slow_work():
    with pytest.raises(ps.TimeoutExceeded):
        ps.safe_timeout(lambda: time.sleep(5), 0.2, what="sleep")


def test_safe_timeout_propagates_the_inner_exception():
    def boom():
        raise ValueError("inner")

    with pytest.raises(ValueError, match="inner"):
        ps.safe_timeout(boom, 5)


def test_safe_timeout_works_from_a_worker_thread():
    """This is the regression that mattered: MCP servers run tools in a
    threadpool, and `signal.alarm` raises ValueError off the main thread."""
    box = {}

    def worker():
        try:
            box["value"] = ps.safe_timeout(lambda: "ok", 5)
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert box.get("value") == "ok", box


def test_safe_timeout_does_not_need_sigalrm(monkeypatch):
    """Windows has no SIGALRM at all — simulate that and check we still work."""
    monkeypatch.delattr(ps, "signal", raising=False)
    assert ps.safe_timeout(lambda: 1, 5) == 1


# --- subprocess -----------------------------------------------------------
def test_run_captures_output_and_exit_code():
    result = ps.run([sys.executable, "-c", "print('hello')"])
    assert result.ok and "hello" in result.output


def test_run_reports_nonzero_without_raising():
    result = ps.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert result.ok is False and result.returncode == 3


def test_run_handles_non_ascii_output():
    """cp1252 on Windows used to raise UnicodeDecodeError inside subprocess."""
    result = ps.run([sys.executable, "-c", "print('कुर्सी ✓ 椅子')"])
    assert result.ok
    assert "कुर्सी" in result.output


def test_run_reports_missing_binary_cleanly():
    result = ps.run(["forge3d-definitely-not-installed"])
    assert result.ok is False
    assert "not found" in result.output.lower()


def test_run_times_out_without_raising():
    result = ps.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
    assert result.ok is False and result.timed_out is True


# --- binary discovery -----------------------------------------------------
def test_find_binary_finds_the_interpreter():
    found = ps.find_binary(Path(sys.executable).name)
    assert found


def test_find_binary_honours_env_override(monkeypatch):
    monkeypatch.setenv("FORGE3D_TESTBIN", sys.executable)
    assert ps.find_binary("testbin", env_var="FORGE3D_TESTBIN") == sys.executable


def test_find_binary_returns_none_for_nonsense():
    assert ps.find_binary("forge3d-no-such-binary-xyz") is None


def test_install_hint_is_platform_specific():
    hint = ps.install_hint("openscad")
    assert hint and "openscad" in hint.lower()
    if ps.IS_WINDOWS:
        assert "winget" in hint
    elif ps.IS_MACOS:
        assert "brew" in hint
    else:
        assert "apt" in hint or "snap" in hint


# --- filesystem -----------------------------------------------------------
def test_atomic_write_replaces_content(tmp_path):
    target = tmp_path / "a.txt"
    ps.atomic_write(target, "one")
    ps.atomic_write(target, "two")
    assert target.read_text(encoding="utf-8") == "two"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    ps.atomic_write(tmp_path / "b.txt", "x")
    assert [p.name for p in tmp_path.iterdir()] == ["b.txt"]


def test_atomic_write_accepts_bytes(tmp_path):
    p = ps.atomic_write(tmp_path / "c.bin", b"\x00\x01")
    assert p.read_bytes() == b"\x00\x01"


def test_writable_dir_detects_a_real_directory(tmp_path):
    assert ps.writable_dir(tmp_path / "sub" / "deeper") is True


def test_writable_dir_false_for_a_file(tmp_path):
    f = tmp_path / "file"
    f.write_text("x", encoding="utf-8")
    assert ps.writable_dir(f) is False


# --- filenames ------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Side Chair!!", "side-chair"),
        ("Chaise Longue #2", "chaise-longue-2"),
        ("chair_2026", "chair-2026"),
        ("  spaced  out  ", "spaced-out"),
    ],
)
def test_slug_basics(raw, expected):
    assert ModelSpec(name=raw).name == expected


def test_slug_preserves_devanagari_with_matras():
    """`\\w` drops combining marks, which mangled "कुर्सी" into "करस"."""
    assert ModelSpec(name="कुर्सी").name == "कुर्सी"


def test_slug_preserves_cjk():
    assert ModelSpec(name="椅子").name == "椅子"


def test_slug_preserves_tamil():
    assert ModelSpec(name="நாற்காலி").name == "நாற்காலி"


@pytest.mark.parametrize("reserved", ["con", "aux", "nul", "COM1", "lpt9"])
def test_slug_avoids_windows_reserved_names(reserved):
    """These are illegal as filenames on Windows only — classic Linux-CI escapee."""
    name = ModelSpec(name=reserved).name
    assert name != reserved.lower()
    assert name.endswith("-model")


def test_slug_never_returns_empty():
    assert ModelSpec(name="!!!").name == "model"
    assert ModelSpec(name="").name == "model"


def test_slug_strips_path_separators():
    name = ModelSpec(name="../../etc/passwd").name
    assert "/" not in name and "\\" not in name and not name.startswith(".")


# --- doctor ---------------------------------------------------------------
def test_doctor_reports_the_current_platform(tmp_path):
    report = ps.doctor(tmp_path)
    assert report["platform"]["system"]
    assert report["platform"]["python"].startswith("3.")
    assert report["platform"]["is_windows"] is ps.IS_WINDOWS


def test_doctor_marks_procedural_available(tmp_path):
    report = ps.doctor(tmp_path)
    assert report["engines"]["procedural"]["available"] is True
    assert report["ready"] is True


def test_doctor_reports_workdir_writability(tmp_path):
    report = ps.doctor(tmp_path / "work")
    assert report["workdir"]["writable"] is True


def test_doctor_hints_are_actionable(tmp_path):
    report = ps.doctor(tmp_path)
    for hint in report["hints"]:
        assert isinstance(hint, str) and len(hint) > 10


def test_doctor_env_report_never_leaks_values(tmp_path, monkeypatch):
    """Values may be secrets (an API key); only presence is reported."""
    monkeypatch.setenv("FORGE3D_HOSTED_KEY", "sk-super-secret-value")
    report = ps.doctor(tmp_path)
    assert "sk-super-secret-value" not in str(report)


def test_windows_candidates_do_not_crash_on_posix():
    assert isinstance(ps._windows_candidates("openscad"), list)
    assert isinstance(ps._macos_candidates("blender"), list)
    assert isinstance(ps._linux_candidates("openscad"), list)


def test_create_flags_is_zero_off_windows():
    if not ps.IS_WINDOWS:
        assert ps._CREATE_NO_WINDOW == 0


def test_path_handling_uses_os_separator(tmp_path):
    p = ps.atomic_write(tmp_path / "nested" / "dir" / "x.txt", "ok")
    assert p.exists() and os.sep in str(p)
