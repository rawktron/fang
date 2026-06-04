"""Tests for config priority chain and discovery integration."""
import textwrap
from pathlib import Path

import pytest

from fang.config import RawConfig, RawProject, ConfigError, resolve
from fang.discovery import DiscoveredProject, EntrypointCandidate


def _raw(name=None, entry=None, python=None) -> RawConfig:
    raw = RawConfig()
    raw.project = RawProject(name=name, entry=entry, python=python)
    return raw


def _discovered(name=None, entry_module=None, python=None) -> DiscoveredProject:
    disc = DiscoveredProject()
    disc.name = name
    disc.version = "1.0.0"
    if entry_module:
        disc.entrypoints = [EntrypointCandidate(module=entry_module, source="project.scripts")]
    if python:
        disc.python_requires = f">={python}"
    return disc


HOST = "macos-arm64"


class TestConfigPriorityChain:
    def test_cli_flag_overrides_fang_toml(self):
        raw = _raw(name="from_toml", entry="from_toml.__main__", python="3.12")
        cfg = resolve(raw, cli_name="cli_name", host_platform=HOST)
        assert cfg.project.name == "cli_name"

    def test_cli_entry_overrides_fang_toml(self):
        raw = _raw(name="app", entry="from_toml.__main__", python="3.12")
        cfg = resolve(raw, cli_entry="cli_entry.__main__", host_platform=HOST)
        assert cfg.project.entry == "cli_entry.__main__"

    def test_cli_python_overrides_fang_toml(self):
        raw = _raw(name="app", entry="app.__main__", python="3.12")
        cfg = resolve(raw, cli_python="3.11", host_platform=HOST)
        assert cfg.project.python == "3.11"

    def test_fang_toml_overrides_discovery(self):
        raw = _raw(name="from_toml", entry="app.__main__", python="3.12")
        disc = _discovered(name="from_discovery", entry_module="disc.__main__")
        cfg = resolve(raw, host_platform=HOST, discovered=disc)
        assert cfg.project.name == "from_toml"

    def test_discovery_fills_missing_fang_toml_fields(self):
        raw = _raw()  # all None
        disc = _discovered(name="myapp", entry_module="myapp.__main__", python="3.12")
        cfg = resolve(raw, host_platform=HOST, discovered=disc)
        assert cfg.project.name == "myapp"
        assert cfg.project.entry == "myapp.__main__"
        assert cfg.project.python == "3.13.0"  # >=3.12 → highest satisfying series (3.13) → default patch

    def test_missing_required_field_names_the_field(self):
        raw = _raw(entry="app.__main__", python="3.12")  # name missing
        with pytest.raises(ConfigError, match="project.name"):
            resolve(raw, host_platform=HOST)

    def test_missing_entry_names_the_field(self):
        raw = _raw(name="app", python="3.12")  # entry missing
        with pytest.raises(ConfigError, match="project.entry"):
            resolve(raw, host_platform=HOST)

    def test_no_fang_toml_uses_discovery_only(self):
        disc = _discovered(name="justpython", entry_module="justpython.__main__", python="3.13")
        cfg = resolve(None, host_platform=HOST, discovered=disc)
        assert cfg.project.name == "justpython"
        assert cfg.project.python == "3.13.0"

    def test_entry_with_callable_parsed(self):
        raw = _raw(name="app", entry="app.main:run", python="3.12")
        cfg = resolve(raw, host_platform=HOST)
        assert cfg.project.entry == "app.main"
        assert cfg.project.entry_callable == "run"

    def test_default_target_is_host(self):
        raw = _raw(name="app", entry="app.__main__", python="3.12")
        cfg = resolve(raw, host_platform=HOST)
        assert cfg.target_platform == HOST


class TestFangTomlRead:
    def test_reads_fang_toml(self, tmp_path):
        toml = tmp_path / "fang.toml"
        toml.write_text(textwrap.dedent("""\
            [project]
            name = "demo"
            entry = "demo.__main__"
            python = "3.12"
        """))
        raw = RawConfig.from_file(toml)
        assert raw is not None
        assert raw.project.name == "demo"
        assert raw.project.python == "3.12"

    def test_missing_file_returns_none(self, tmp_path):
        raw = RawConfig.from_file(tmp_path / "fang.toml")
        assert raw is None


class TestVenvDiscovery:
    def test_ignores_virtual_env_outside_project(self, tmp_path, monkeypatch):
        runner_venv = tmp_path / "runner" / ".venv"
        runner_venv.mkdir(parents=True)
        (runner_venv / "pyvenv.cfg").write_text("version_info = 3.12.3\n")

        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("VIRTUAL_ENV", str(runner_venv))

        discovered = DiscoveredProject.from_directory(project)

        assert discovered.venv_path is None
        assert discovered.venv_python_version is None

    def test_uses_virtual_env_inside_project(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        venv = project / ".venv"
        venv.mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("version_info = 3.12.3\n")
        monkeypatch.setenv("VIRTUAL_ENV", str(venv))

        discovered = DiscoveredProject.from_directory(project)

        assert discovered.venv_path == str(venv.resolve())
        assert discovered.venv_python_version == "3.12.3"
