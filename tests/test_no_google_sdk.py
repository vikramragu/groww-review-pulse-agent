"""P6-T1: no Google SDK, anywhere (problemStatement.md §8, the last two checklist lines).

All Google access goes through the MCP server, which owns OAuth, tokens and REST calls
(architecture.md D8). These tests fail the moment a Google API or auth library enters this
repo by any route: declared in `pyproject.toml`, pulled in transitively and installed, or
imported in source.

`google-play-scraper` (the optional `scraper` extra for `pulse fetch`) is allowed: it reads
public store pages over plain HTTP, has no dependencies, and holds no Google credential.
"""

from __future__ import annotations

import ast
import importlib.metadata
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Distributions that would mean a direct Google API or OAuth path.
FORBIDDEN_DISTRIBUTIONS = {
    "google-api-python-client",
    "google-api-core",
    "google-auth",
    "google-auth-oauthlib",
    "google-auth-httplib2",
    "google-genai",
    "google-generativeai",
    "oauth2client",
    "gspread",
}
FORBIDDEN_PREFIXES = ("google-cloud-", "google-apps-")
ALLOWED = {"google-play-scraper"}

#: Top-level imports that would mean the same thing in source.
FORBIDDEN_IMPORTS = (
    "googleapiclient",
    "google.auth",
    "google.oauth2",
    "oauth2client",
    "google.cloud",
)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _forbidden(name: str) -> bool:
    normalized = _normalize(name)
    if normalized in ALLOWED:
        return False
    return normalized in FORBIDDEN_DISTRIBUTIONS or normalized.startswith(FORBIDDEN_PREFIXES)


def _requirement_name(spec: str) -> str:
    return re.split(r"[\s<>=!~;\[(]", spec.strip(), maxsplit=1)[0]


def test_pyproject_declares_no_google_sdk() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        declared.extend(extra)

    offending = sorted(name for name in map(_requirement_name, declared) if _forbidden(name))
    assert not offending, f"Google SDK declared in pyproject.toml: {offending}"


def test_no_google_sdk_is_installed_in_the_dependency_tree() -> None:
    """Catches a transitive dependency too: whatever the declared packages pulled in."""
    installed = {dist.metadata["Name"] for dist in importlib.metadata.distributions()}
    offending = sorted(name for name in installed if name and _forbidden(name))
    assert not offending, f"Google SDK installed in this environment: {offending}"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


@pytest.mark.parametrize(
    "path",
    sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py")),
    ids=lambda p: str(p.relative_to(ROOT)),
)
def test_no_source_file_imports_a_google_sdk(path: Path) -> None:
    offending = sorted(
        name
        for name in _imports(path)
        if any(name == banned or name.startswith(banned + ".") for banned in FORBIDDEN_IMPORTS)
    )
    assert not offending, f"{path.relative_to(ROOT)} imports {offending}"


@pytest.mark.parametrize(
    "name", ["google-auth", "google_api_python_client", "google-cloud-storage", "OAuth2Client"]
)
def test_the_check_recognises_sdk_names(name: str) -> None:
    """Guards the guard: a normalisation bug would make every test above pass vacuously."""
    assert _forbidden(name)


def test_the_store_scraper_is_allowed() -> None:
    assert not _forbidden("google-play-scraper")
