"""JSON output tests for init, check, doctor, inspect commands."""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest
from click.testing import CliRunner

from fang.cli import cli
from fang.archive import ArchiveWriter, Meta
from fang.tests.test_runtime import _make_exe_with_trailer


# ── helpers ──────────────────────────────────────────────────────────────────────

def _invoke(args: list[str], env_extra: dict | None = None) -> tuple[int, dict]:
    """Invoke CLI and parse JSON output."""
    runner = CliRunner()
    env = {**os.environ, "CI": "1"}  # suppress fancy output
    if env_extra:
        env.update(env_extra)
    result = runner.invoke(cli, args, catch_exceptions=False, env=env)
    lines = [l for l in result.output.strip().splitlines() if l.startswith("{")]
    assert lines, f"no JSON in output: {result.output!r}"
    return result.exit_code, json.loads(lines[-1])


def _make_fang_archive(runtime_bytes: bytes = b"fake runtime") -> bytes:
    w = ArchiveWriter()
    w.add_bytes("native-libs/fang-runtime-3.12-aarch64-apple-darwin", runtime_bytes)
    w.set_meta(Meta(
        python_version="3.12.3",
        entry_point="testapp.__main__",
        platform="macos-arm64",
        build_timestamp="2024-01-01T00:00:00Z",
        project_name="testapp",
    ))
    return w.build()


# ── init ──────────────────────────────────────────────────────────────────────────

class TestInitJson:
    def test_success(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "testapp"\nversion = "1.0.0"\n'
            'requires-python = ">=3.12"\n'
            '[project.scripts]\ntestapp = "testapp.__main__:main"\n'
        )
        (tmp_path / "testapp").mkdir()
        (tmp_path / "testapp" / "__init__.py").write_bytes(b"")
        (tmp_path / "testapp" / "__main__.py").write_bytes(b"def main(): pass")

        code, data = _invoke(["--json", "init"])
        assert data["command"] == "init"
        assert data["status"] == "ok"

    def test_already_exists_without_force(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "fang.toml").write_text('[project]\nname = "x"\n')

        code, data = _invoke(["--json", "init"])
        assert data["status"] == "error"
        assert code != 0


# ── check ─────────────────────────────────────────────────────────────────────────

class TestCheckJson:
    def test_pass_with_valid_project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'requires-python = ">=3.12"\n'
            '[project.scripts]\nmyapp = "myapp.__main__:main"\n'
        )
        (tmp_path / "myapp").mkdir()
        (tmp_path / "myapp" / "__init__.py").write_bytes(b"")

        code, data = _invoke(["--json", "check"])
        assert data["command"] == "check"
        assert data["status"] in ("ok", "warn")

    def test_fail_no_project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        code, data = _invoke(["--json", "check"])
        assert data["status"] == "fail"
        assert code != 0

    def test_warn_multiple_entrypoints(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "1.0.0"\n'
            'requires-python = ">=3.12"\n'
            '[project.scripts]\n'
            'myapp = "myapp.__main__:main"\n'
            'myapp2 = "myapp2.__main__:main"\n'
        )
        for pkg in ("myapp", "myapp2"):
            (tmp_path / pkg).mkdir()
            (tmp_path / pkg / "__init__.py").write_bytes(b"")

        code, data = _invoke(["--json", "check"])
        assert data["status"] in ("warn", "fail")



# ── inspect ───────────────────────────────────────────────────────────────────────

class TestInspectJson:
    def _make_exe(self, tmp_path: Path) -> Path:
        archive = _make_fang_archive()
        exe = tmp_path / "fang-binary"
        exe.write_bytes(_make_exe_with_trailer(archive))
        return exe

    def test_valid_executable(self, tmp_path):
        exe = self._make_exe(tmp_path)
        code, data = _invoke(["--json", "inspect", str(exe)])
        assert data["command"] == "inspect"
        assert data["status"] == "ok"
        assert data["payload"]["meta"]["project_name"] == "testapp"

    def test_list_assets(self, tmp_path):
        exe = self._make_exe(tmp_path)
        code, data = _invoke(["--json", "inspect", "--list-assets", str(exe)])
        assert "assets" in data["payload"]
        assert len(data["payload"]["assets"]) > 0

    def test_verify_passes_on_intact_archive(self, tmp_path):
        exe = self._make_exe(tmp_path)
        code, data = _invoke(["--json", "inspect", "--verify", str(exe)])
        assert data["payload"]["verify_failed"] == []
