"""Release metadata stays consistent: one version, everywhere it is published.

release.yml tags and publishes images from pyproject.toml's version, so every
manifest, install command, and changelog heading must name that version or
users install images that do not exist.
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION: str = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
    "version"
]
_DOCS = ["README.md", "examples/README.md", "sample-workload/README.md"]
_MANIFESTS = ["k8s/scalescope/deployment.yaml", "sample-workload/k8s/deployment.yaml"]


def _release_notes_module() -> Any:
    path = ROOT / "scripts" / "release_notes.py"
    spec = importlib.util.spec_from_file_location("release_notes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_changelog_leads_with_the_current_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text()
    # Unreleased entries collect above the current version between releases.
    headings = re.findall(r"^## \[([^\]]+)\]", changelog, re.MULTILINE)
    released = [heading for heading in headings if heading != "Unreleased"]
    assert released[0] == VERSION
    assert _release_notes_module().release_notes(changelog, VERSION)


@pytest.mark.parametrize("path", _MANIFESTS)
def test_manifests_pin_the_current_image(path: str) -> None:
    tags = re.findall(r"image: ghcr\.io/[\w-]+/[\w-]+:(\S+)", (ROOT / path).read_text())
    assert tags == [VERSION]


@pytest.mark.parametrize("path", _DOCS)
def test_install_commands_use_the_current_release(path: str) -> None:
    text = (ROOT / path).read_text()
    refs = set(re.findall(r"\?ref=v([0-9][\w.-]*)", text))
    images = set(re.findall(r"/scalescope[\w-]*:([0-9][\w.-]*)", text))
    assert refs | images <= {VERSION}


def test_release_notes_are_the_versions_section_only() -> None:
    changelog = "# Changelog\n\n## [2.0.0]\n\n- new\n\n## [1.0.0]\n\n- old\n"
    notes = _release_notes_module().release_notes(changelog, "2.0.0")
    assert notes == "- new"


@pytest.mark.parametrize(
    "changelog", ["## [1.0.0]\n\n- old\n", "## [2.0.0]\n\n## [1.0.0]\n\n- old\n"]
)
def test_release_notes_refuse_a_missing_or_empty_section(changelog: str) -> None:
    with pytest.raises(ValueError):
        _release_notes_module().release_notes(changelog, "2.0.0")
