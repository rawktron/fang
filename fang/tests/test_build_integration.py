"""Integration test: build a real fixture project and run the output binary.

Skipped when FANG_RUNTIME_PATH is not set — requires a compiled fang-runtime binary.
Run locally with:
  FANG_RUNTIME_PATH=./target/release/fang-runtime uv run pytest fang/tests/test_build_integration.py -v
"""
from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest


FANG_RUNTIME_PATH = os.environ.get("FANG_RUNTIME_PATH")

pytestmark = pytest.mark.skipif(
    not FANG_RUNTIME_PATH,
    reason="FANG_RUNTIME_PATH not set — skipping integration tests",
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


@pytest.fixture()
def fixture_project(tmp_path: Path) -> Path:
    """A minimal hello-world Python project buildable by fang."""
    _write(tmp_path / "pyproject.toml", """\
        [project]
        name = "hello"
        version = "0.1.0"
    """)
    _write(tmp_path / "fang.toml", """\
        [project]
        name = "hello"
        entry = "hello.__main__"
        python = "3.12"
    """)
    _write(tmp_path / "hello/__init__.py", "")
    _write(tmp_path / "hello/__main__.py", """\
        import sys
        print("hello from fang")
        sys.exit(0)
    """)
    return tmp_path


def test_build_produces_executable(fixture_project: Path, tmp_path: Path):
    output = tmp_path / "hello-bin"
    result = subprocess.run(
        ["python", "-m", "fang", "build", "--output", str(output), str(fixture_project)],
        env={**os.environ, "FANG_RUNTIME_PATH": FANG_RUNTIME_PATH},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"fang build failed:\n{result.stderr}"
    assert output.exists(), "output binary not created"
    assert output.stat().st_mode & stat.S_IEXEC, "output binary not executable"


def test_built_binary_runs(fixture_project: Path, tmp_path: Path):
    output = tmp_path / "hello-bin"
    subprocess.run(
        ["python", "-m", "fang", "build", "--output", str(output), str(fixture_project)],
        env={**os.environ, "FANG_RUNTIME_PATH": FANG_RUNTIME_PATH},
        capture_output=True, check=True,
    )
    run = subprocess.run([str(output)], capture_output=True, text=True)
    assert run.returncode == 0, f"binary exited {run.returncode}:\n{run.stderr}"
    assert "hello from fang" in run.stdout


def test_built_binary_exits_nonzero_on_app_error(fixture_project: Path, tmp_path: Path):
    (fixture_project / "hello/__main__.py").write_text(
        "import sys; sys.exit(42)\n"
    )
    output = tmp_path / "hello-bin"
    subprocess.run(
        ["python", "-m", "fang", "build", "--output", str(output), str(fixture_project)],
        env={**os.environ, "FANG_RUNTIME_PATH": FANG_RUNTIME_PATH},
        capture_output=True, check=True,
    )
    run = subprocess.run([str(output)], capture_output=True)
    assert run.returncode == 42
