"""The consumer-driven contract with the Docs/Gmail MCP server, as data.

`contract/google-workspace-mcp-0.1.json` records what this agent sends to the server and
which response fields it reads -- not the server's full schemas, which belong to the
servers project. Every request is validated against it before sending, every reply before
use (architecture.md §18), and `pulse contract check` asserts the live server still
accepts our example requests and still declares the fields we read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for

DEFAULT_CONTRACT_PATH = Path("contract/google-workspace-mcp-0.1.json")

_SEMVER_RE = re.compile(r"^v?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?(?:[-+].*)?$")


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"
    FEATURE_DISABLED = "FEATURE_DISABLED"
    #: Google was unavailable even after the server's own retries; worth retrying later.
    UNAVAILABLE = "UNAVAILABLE"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"


class ContractError(Exception):
    """The contract file is missing or malformed, or a version string cannot be parsed."""


@dataclass(frozen=True)
class ServerVersion:
    major: int
    minor: int

    @classmethod
    def parse(cls, raw: str) -> ServerVersion:
        match = _SEMVER_RE.match(raw.strip())
        if match is None:
            raise ContractError(
                f"Cannot parse server version {raw!r}; expected semver like '0.1.0'."
            )
        return cls(int(match["major"]), int(match["minor"]))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


@dataclass(frozen=True)
class ToolContract:
    name: str
    role: str
    request: dict[str, Any]
    response: dict[str, Any]
    example: dict[str, Any]


@dataclass(frozen=True)
class Contract:
    server: str
    version: ServerVersion
    tools: dict[str, ToolContract]
    error_rules: list[tuple[re.Pattern[str], ErrorCode]]
    fallback: ErrorCode
    never_bind: list[str]

    def tool_for_role(self, role: str) -> ToolContract:
        for tool in self.tools.values():
            if tool.role == role:
                return tool
        raise ContractError(f"The contract defines no tool for role {role!r}.")

    @cached_property
    def _validators(self) -> dict[tuple[str, str], Draft202012Validator]:
        validators: dict[tuple[str, str], Draft202012Validator] = {}
        for tool in self.tools.values():
            validators[("request", tool.name)] = Draft202012Validator(tool.request)
            validators[("response", tool.name)] = Draft202012Validator(tool.response)
        return validators

    def violations(self, kind: str, tool: str, payload: Any) -> list[str]:
        """Human-readable schema violations; empty when `payload` conforms."""
        return schema_violations(self._validators[(kind, tool)], payload)

    def classify_error(self, message: str) -> ErrorCode:
        for pattern, code in self.error_rules:
            if pattern.search(message):
                return code
        return self.fallback

    def forbids(self, tool_name: str) -> bool:
        lowered = tool_name.lower()
        return any(fragment in lowered for fragment in self.never_bind)


def schema_violations(validator: Draft202012Validator, payload: Any) -> list[str]:
    return [
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    ]


def load_contract(path: Path = DEFAULT_CONTRACT_PATH) -> Contract:
    if not path.exists():
        raise ContractError(f"Contract file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tools = {
            name: ToolContract(
                name=name,
                role=spec["role"],
                request=spec["request"],
                response=spec["response"],
                example=spec["example"],
            )
            for name, spec in data["tools"].items()
        }
        rules = [
            (re.compile(rule["match"]), ErrorCode(rule["code"])) for rule in data["errors"]["rules"]
        ]
        contract = Contract(
            server=str(data["server"]),
            version=ServerVersion.parse(str(data["version"])),
            tools=tools,
            error_rules=rules,
            fallback=ErrorCode(data["errors"]["fallback"]),
            never_bind=[str(f).lower() for f in data.get("never_bind", [])],
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, re.error) as exc:
        raise ContractError(f"Malformed contract file {path}: {exc}") from exc

    for tool in tools.values():
        Draft202012Validator.check_schema(tool.request)
        Draft202012Validator.check_schema(tool.response)
        if problems := contract.violations("request", tool.name, tool.example):
            raise ContractError(
                f"{path}: the example for {tool.name} violates its own request: {problems}"
            )
        if contract.forbids(tool.name):
            raise ContractError(
                f"{path}: tool {tool.name!r} matches never_bind {contract.never_bind}"
            )
    return contract


def request_schema_problems(advertised: dict[str, Any], example: dict[str, Any]) -> list[str]:
    """Whether a server's advertised input schema accepts one of our example requests.

    The validator follows the schema's own `$schema` -- a TypeScript server built on zod
    publishes draft-07, not 2020-12.
    """
    validator_class = validator_for(advertised, default=Draft202012Validator)
    return schema_violations(validator_class(advertised), example)
