import importlib.metadata as metadata
import json
import sys
import types

import pytest

sys.path.insert(0, "python")

from fang_compat import install, uninstall  # noqa: E402
from fang_compat.metadata import FangDistributionFinder, normalize_name  # noqa: E402


@pytest.fixture(autouse=True)
def clean_fang_compat():
    uninstall()
    previous = sys.modules.pop("pkg_resources", None)
    try:
        yield
    finally:
        uninstall()
        sys.modules.pop("pkg_resources", None)
        if previous is not None and not getattr(previous, "__fang_compat__", False):
            sys.modules["pkg_resources"] = previous


def sample_manifest():
    return {
        "distributions": [
            {
                "name": "Demo.Pkg",
                "version": "1.2.3",
                "summary": "demo summary",
                "top_level": ["demo_pkg"],
                "files": ["demo_pkg/__init__.py", "demo_pkg/cli.py"],
                "requires": ["requests>=2", "typing-extensions; python_version < '3.12'"],
                "entry_points": {
                    "console_scripts": {"demo-cli": "demo_pkg.cli:main"},
                    "fang.plugins": {"demo": "demo_pkg.plugin:Plugin"},
                },
                "metadata": {"Author": "Fang Team"},
            }
        ]
    }


def test_install_from_manifest_exposes_importlib_metadata():
    install(manifest=sample_manifest())

    assert normalize_name("Demo_Pkg") == "demo-pkg"
    assert metadata.version("demo_pkg") == "1.2.3"

    dist = metadata.distribution("DEMO-PKG")
    assert dist.metadata["Name"] == "Demo.Pkg"
    assert dist.metadata["Summary"] == "demo summary"
    assert dist.metadata["Author"] == "Fang Team"
    assert dist.read_text("top_level.txt") == "demo_pkg\n"
    assert "requests>=2" in dist.requires

    files = {str(path) for path in dist.files}
    assert "demo_pkg/__init__.py" in files
    assert "site-packages/demo_pkg-1.2.3.dist-info/METADATA" in files


def test_importlib_entry_points_and_packages_distributions_include_virtual_dist():
    install(manifest=sample_manifest())

    entry_points = metadata.entry_points()
    selected = tuple(entry_points.select(group="console_scripts", name="demo-cli"))
    assert len(selected) == 1
    assert selected[0].value == "demo_pkg.cli:main"

    packages = metadata.packages_distributions()
    assert "Demo.Pkg" in packages["demo_pkg"]


def test_install_from_archive_uses_dist_info_file_contents():
    manifest = {
        "distributions": [
            {
                "name": "Archive-Pkg",
                "version": "4.5.6",
                "dist_info": "site-packages/archive_pkg-4.5.6.dist-info",
                "top_level": ["archive_pkg"],
            }
        ]
    }
    archive = {
        "meta/distributions.json": json.dumps(manifest).encode(),
        "site-packages/archive_pkg-4.5.6.dist-info/METADATA": (
            "Metadata-Version: 2.1\n"
            "Name: Archive-Pkg\n"
            "Version: 4.5.6\n"
            "Summary: from archive\n"
        ).encode(),
        "site-packages/archive_pkg-4.5.6.dist-info/entry_points.txt": (
            "[console_scripts]\narchive-cli = archive_pkg.cli:main\n"
        ).encode(),
    }

    install(archive=archive)

    dist = metadata.distribution("archive_pkg")
    assert dist.metadata["Summary"] == "from archive"
    selected = tuple(metadata.entry_points().select(group="console_scripts", name="archive-cli"))
    assert len(selected) == 1
    assert selected[0].value == "archive_pkg.cli:main"


def test_pkg_resources_get_distribution_and_metadata():
    install(manifest=sample_manifest())

    import pkg_resources

    dist = pkg_resources.get_distribution("demo-pkg")
    assert dist.project_name == "Demo.Pkg"
    assert dist.key == "demo-pkg"
    assert dist.version == "1.2.3"
    assert dist.has_metadata("METADATA")
    assert "Name: Demo.Pkg" in dist.get_metadata("METADATA")
    assert any(line == "demo_pkg" for line in dist.get_metadata_lines("top_level.txt"))
    assert pkg_resources.working_set.find("demo-pkg").project_name == "Demo.Pkg"


def test_pkg_resources_iter_entry_points_and_load():
    install(manifest=sample_manifest())

    module = types.ModuleType("demo_pkg.cli")

    def main():
        return "loaded"

    module.main = main
    sys.modules["demo_pkg.cli"] = module

    try:
        import pkg_resources

        [entry_point] = list(pkg_resources.iter_entry_points("console_scripts", "demo-cli"))
        assert entry_point.name == "demo-cli"
        assert entry_point.module_name == "demo_pkg.cli"
        assert entry_point.load()() == "loaded"
        assert pkg_resources.load_entry_point("demo-pkg", "console_scripts", "demo-cli")() == "loaded"
    finally:
        sys.modules.pop("demo_pkg.cli", None)


def test_pkg_resources_require_and_missing_distribution():
    install(manifest=sample_manifest())

    import pkg_resources

    [dist] = pkg_resources.require("demo-pkg>=1")
    assert dist.project_name == "Demo.Pkg"

    with pytest.raises(pkg_resources.DistributionNotFound):
        pkg_resources.get_distribution("missing-pkg")

    with pytest.raises(metadata.PackageNotFoundError):
        metadata.version("missing-pkg")


def test_install_is_idempotent_for_fang_finder():
    install(manifest=sample_manifest())
    install(manifest=sample_manifest())

    finders = [item for item in sys.meta_path if isinstance(item, FangDistributionFinder)]
    assert len(finders) == 1
