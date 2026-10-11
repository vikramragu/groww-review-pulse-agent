"""Shared test setup.

Two guarantees the whole suite relies on:

1. Tests run from the repository root, so relative paths like `config/` resolve
   regardless of where pytest was invoked.
2. No test sees an ambient `PULSE_*` or `ANTHROPIC_*` environment variable. Without this,
   a developer's own exported key or input path silently changes what the suite asserts —
   and the failure only shows up on someone else's machine.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Stripped from every test's environment. `GROQ_` matters as much as the others: a
#: developer with a real key exported would otherwise have the suite construct a live
#: client, and the tests are supposed to be offline everywhere.
MANAGED_ENV_PREFIXES = ("PULSE_", "ANTHROPIC_", "GROQ_")


@pytest.fixture(autouse=True)
def _repo_root_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)


@pytest.fixture(autouse=True)
def _no_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Point the `.env` loader at a path that does not exist.

    Without this, a developer with a real key in `./.env` would have the suite load it --
    and tests that assert "no credential configured" would pass or fail depending on whose
    machine they ran on.
    """
    monkeypatch.setenv("PULSE_ENV_FILE", str(tmp_path_factory.mktemp("noenv") / "absent.env"))


@pytest.fixture(autouse=True)
def _clean_environment() -> Iterator[None]:
    removed = {
        name: value for name, value in os.environ.items() if name.startswith(MANAGED_ENV_PREFIXES)
    }
    for name in removed:
        del os.environ[name]
    try:
        yield
    finally:
        os.environ.update(removed)
