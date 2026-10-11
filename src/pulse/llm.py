"""The LLM port and its implementations (P2-T1).

Only stages 4 and 6 call a model, and every hard constraint from the brief is enforced in
deterministic code rather than by the model (architecture.md §3). That makes the provider a
configuration choice, so this module is the single place that knows which one is in use.

Three implementations:

* `GroqLlm` — the default (D6a). Groq's API is OpenAI-shaped.
* `FakeLlm` — scripted responses for tests. Without it the stage-6 regeneration loop would
  be untestable: P3-T18 needs to script "return a paraphrased quote, then a verbatim one"
  and assert the loop behaves.
* `NullLlm` — fails loudly if a stage reaches a model without one being configured.

**Validation is always client-side.** `generate_json` ships a JSON Schema to the provider,
but the caller re-validates the response against the Pydantic model regardless. Schema
support on Groq is model-dependent and the published accounts of it disagree, so trusting
the provider to enforce it would be building on sand. This way a provider that only offers
best-effort JSON degrades to "more retries", never to wrong data.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from pulse import logging as pulse_logging

log = pulse_logging.get_logger("llm")

ModelT = TypeVar("ModelT", bound=BaseModel)

GROQ_API_KEY_ENV = "GROQ_API_KEY"
ANTHROPIC_API_KEY_ENVS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

#: Published per-million-token prices, for the ledger's cost estimate only. Rates move and
#: vary by model, so this is explicitly an estimate — `None` means "unknown, report tokens
#: without a dollar figure" rather than silently reporting zero.
PRICE_TABLE: dict[str, tuple[float, float]] = {}


class LlmError(Exception):
    """Raised when a provider call fails in a way the caller cannot retry past."""


class LlmNotConfigured(LlmError):
    """Raised when a stage needs a model but no provider is configured."""


class LlmResponseInvalid(LlmError):
    """Raised when a response is not valid JSON, or does not match the schema.

    Retryable: the caller decides whether to re-ask. Carries the raw text so a retry can
    quote the specific failure back to the model rather than saying "try again".
    """

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


@dataclass
class LlmUsage:
    """Token accounting across a run. Mirrors `models.LlmUsage` but mutable."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def add(self, *, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cached_input_tokens += cached_input_tokens

    def estimated_cost_usd(self, model: str) -> float | None:
        prices = PRICE_TABLE.get(model)
        if prices is None:
            return None
        input_rate, output_rate = prices
        return (self.input_tokens * input_rate + self.output_tokens * output_rate) / 1_000_000


@dataclass
class LlmResponse:
    """One completion. `text` is expected to be a JSON document."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    #: True when the provider enforced the schema rather than merely being asked to.
    schema_enforced: bool = False


class LlmPort(Protocol):
    """What stages 4 and 6 need from a model, and nothing more."""

    provider: str
    model: str
    #: Whether this provider offers an explicit prompt-cache breakpoint. Groq does not, so
    #: the cache-hit assertion in P2 is conditional on it rather than unconditional.
    supports_prompt_cache: bool

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
        reasoning_effort: str | None = None,
    ) -> LlmResponse:
        """Ask for a JSON document matching `schema`. Never validates it."""
        ...


# --------------------------------------------------------------------------------------
# Schema helpers
# --------------------------------------------------------------------------------------


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for a Pydantic model, reshaped for strict structured-output modes.

    Pydantic's default output is rejected by strict mode for three separate reasons, and
    each 400 reads like "this model doesn't support schemas" rather than naming the real
    cause — which is how a transient schema-shape problem gets mistaken for a provider
    capability and silently downgrades the whole run to unenforced JSON.

    1. **`additionalProperties` / `required`** — strict mode requires every object to set
       `additionalProperties: false` and list all its properties as required.
    2. **`$ref` / `$defs`** — inlined, because a nested definition block is not reliably
       supported and makes the next fix impossible to apply locally.
    3. **Nullable enums** — `Theme | None` becomes `anyOf: [{$ref: Theme}, {type: null}]`,
       and Groq rejects an undiscriminated `anyOf`. Collapsed into a single enum with
       `null` as a member, which is the shape strict mode expects.
    """
    schema = _inline_refs(model.model_json_schema())
    _tighten(schema)
    return schema


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline every `$ref` and collapse nullable-enum unions."""
    definitions = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = str(node["$ref"]).rsplit("/", 1)[-1]
                if name in definitions:
                    return walk(deepcopy(definitions[name]))
                return node

            if "anyOf" in node:
                branches = [walk(branch) for branch in node["anyOf"]]
                nullable = any(branch.get("type") == "null" for branch in branches)
                others = [branch for branch in branches if branch.get("type") != "null"]

                if nullable and len(others) == 1 and "enum" in others[0]:
                    merged = {k: v for k, v in others[0].items() if k != "title"}
                    merged["enum"] = [*merged["enum"], None]
                    # `type` is dropped rather than widened: `enum` already constrains the
                    # value completely, and "type": "string" alongside a null member is a
                    # contradiction some validators reject.
                    merged.pop("type", None)
                    for key, value in node.items():
                        if key != "anyOf":
                            merged.setdefault(key, value)
                    return merged

                rebuilt = {k: walk(v) for k, v in node.items() if k != "anyOf"}
                rebuilt["anyOf"] = branches
                return rebuilt

            return {key: walk(value) for key, value in node.items()}

        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    result = walk(schema)
    return result if isinstance(result, dict) else schema


def _tighten(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"])
        for value in node.values():
            _tighten(value)
    elif isinstance(node, list):
        for item in node:
            _tighten(item)


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> str:
    """Pull a JSON document out of a response that may be wrapped in prose or fences.

    Needed because not every model honours a JSON-only instruction. A model that returns
    valid JSON inside a fenced block has done the useful part of the job; refusing to parse
    it would waste a call for no benefit.
    """
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        return stripped

    if match := _JSON_BLOCK.search(stripped):
        return match.group(1).strip()

    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            return stripped[start : end + 1]

    return stripped


def parse_as(response: LlmResponse, model: type[ModelT]) -> ModelT:
    """Validate a response against the Pydantic model. The only place output is trusted.

    Raises `LlmResponseInvalid`, which the caller may retry.
    """
    payload = _extract_json(response.text)
    try:
        return model.model_validate_json(payload)
    except ValidationError as exc:
        raise LlmResponseInvalid(
            f"{model.__name__} validation failed: {_terse_errors(exc)}", raw=payload
        ) from exc
    except ValueError as exc:
        raise LlmResponseInvalid(f"response was not valid JSON: {exc}", raw=payload) from exc


def _terse_errors(exc: ValidationError, limit: int = 5) -> str:
    """Compact validation errors, suitable for feeding back to a model as a retry hint."""
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors()[:limit]
    ]
    remaining = len(exc.errors()) - len(parts)
    if remaining > 0:
        parts.append(f"(+{remaining} more)")
    return "; ".join(parts)


# --------------------------------------------------------------------------------------
# Groq
# --------------------------------------------------------------------------------------


class GroqLlm:
    """Groq implementation. OpenAI-shaped API via the `groq` SDK.

    Schema handling is adaptive by design. Which Groq models accept
    `response_format: {"type": "json_schema"}` is model-dependent, and the published
    accounts of it contradict each other — so rather than hardcode a belief about the
    model, the first call tries json_schema and falls back to `json_object` permanently
    for this instance if the provider rejects it. The fallback is logged once, and
    client-side validation makes the difference a matter of retry count rather than
    correctness.
    """

    provider = "groq"
    supports_prompt_cache = False  # no explicit cache breakpoint; see architecture.md §8.1

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        temperature: float = 0.0,
        timeout: float = 120.0,
        usage: LlmUsage | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.usage = usage or LlmUsage()
        self._schema_mode_supported: bool | None = None
        self._reasoning_effort_supported = True

        key = api_key or os.environ.get(GROQ_API_KEY_ENV)
        if not key:
            raise LlmNotConfigured(
                f"{GROQ_API_KEY_ENV} is not set. Export it, or run with --llm fake for "
                f"offline development."
            )

        try:
            from groq import Groq
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LlmNotConfigured(
                "The `groq` package is not installed. Run: pip install -e '.[dev]'"
            ) from exc

        self._client = Groq(api_key=key, timeout=timeout)

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
        reasoning_effort: str | None = None,
    ) -> LlmResponse:
        """Two capabilities are learned at runtime rather than assumed.

        Schema mode and `reasoning_effort` are both model-dependent on Groq, and the
        account's model list is not knowable in advance. Each is tried once, and a
        rejection disables it for the life of this instance so the whole corpus is not
        spent rediscovering the same 400.
        """
        want_schema = self._schema_mode_supported is not False
        effort = reasoning_effort if self._reasoning_effort_supported else None

        def attempt(*, use_schema: bool, effort: str | None) -> LlmResponse:
            return self._call(
                system,
                user,
                schema,
                schema_name,
                max_tokens,
                use_schema=use_schema,
                reasoning_effort=effort,
            )

        try:
            return attempt(use_schema=want_schema, effort=effort)
        except _ReasoningEffortUnsupported:
            # Only the reasoning models (the gpt-oss family) accept this parameter.
            self._reasoning_effort_supported = False
            log.info(
                "model does not accept reasoning_effort; omitting it",
                extra={"model": self.model},
            )
            effort = None
            try:
                return attempt(use_schema=want_schema, effort=None)
            except _SchemaUnsupported:
                pass
        except _SchemaUnsupported:
            pass

        self._schema_mode_supported = False
        log.warning(
            "provider rejected json_schema; falling back to json_object for this run",
            extra={"model": self.model, "provider": self.provider},
        )
        return attempt(use_schema=False, effort=effort)

    def _call(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
        *,
        use_schema: bool,
        reasoning_effort: str | None = None,
    ) -> LlmResponse:
        from groq.types.chat.completion_create_params import (
            ResponseFormatResponseFormatJsonObject,
            ResponseFormatResponseFormatJsonSchema,
            ResponseFormatResponseFormatJsonSchemaJsonSchema,
        )

        response_format: (
            ResponseFormatResponseFormatJsonSchema | ResponseFormatResponseFormatJsonObject
        )

        if use_schema:
            response_format = ResponseFormatResponseFormatJsonSchema(
                type="json_schema",
                json_schema=ResponseFormatResponseFormatJsonSchemaJsonSchema(
                    name=schema_name, schema=schema, strict=True
                ),
            )
            system_text = system
        else:
            # Without provider-side enforcement the schema has to go in the prompt, or the
            # model has no idea what shape to produce.
            response_format = ResponseFormatResponseFormatJsonObject(type="json_object")
            system_text = (
                f"{system}\n\n"
                f"Reply with a single JSON object and nothing else. It must validate "
                f"against this JSON Schema:\n{json.dumps(schema, sort_keys=True)}"
            )

        from groq.types.chat import (
            ChatCompletionMessageParam,
            ChatCompletionSystemMessageParam,
            ChatCompletionUserMessageParam,
        )

        messages: list[ChatCompletionMessageParam] = [
            ChatCompletionSystemMessageParam(role="system", content=system_text),
            ChatCompletionUserMessageParam(role="user", content=user),
        ]

        extra: dict[str, Any] = {}
        if reasoning_effort is not None:
            extra["reasoning_effort"] = reasoning_effort

        try:
            completion = self._client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=self.temperature,
                response_format=response_format,
                messages=messages,
                **extra,
            )
        except Exception as exc:
            if reasoning_effort is not None and _is_reasoning_effort_rejection(exc):
                raise _ReasoningEffortUnsupported from exc
            if use_schema and _is_schema_rejection(exc):
                raise _SchemaUnsupported from exc
            raise LlmError(f"Groq request failed ({self.model}): {exc}") from exc

        if use_schema:
            self._schema_mode_supported = True

        text = completion.choices[0].message.content or ""
        usage = getattr(completion, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        self.usage.add(input_tokens=input_tokens, output_tokens=output_tokens)

        return LlmResponse(
            text=text,
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            schema_enforced=use_schema,
        )


class _SchemaUnsupported(Exception):
    """Internal signal: this model rejected a json_schema response_format."""


class _ReasoningEffortUnsupported(Exception):
    """Internal signal: this model does not accept a reasoning_effort parameter."""


def _is_reasoning_effort_rejection(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is not None and int(status) != 400:
        return False
    return "reasoning_effort" in str(exc).lower()


_SCHEMA_REJECTION_MARKERS = (
    "response_format",
    "json_schema",
    "does not support",
    "unsupported",
)


def _is_schema_rejection(exc: Exception) -> bool:
    """Distinguish "this model can't do json_schema" from a real failure.

    Matched on the message because the providers return a generic 400 for it. Deliberately
    narrow: a rate limit or auth failure must not be mistaken for a schema problem and
    silently retried in a weaker mode.
    """
    status = getattr(exc, "status_code", None)
    if status is not None and int(status) != 400:
        return False
    message = str(exc).lower()
    return "json_schema" in message or (
        "response_format" in message
        and any(marker in message for marker in _SCHEMA_REJECTION_MARKERS)
    )


# --------------------------------------------------------------------------------------
# Test and fallback implementations
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# Account probing (`pulse models`)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaProbe:
    """Result of asking one model whether it will honour a json_schema response format."""

    model: str
    supported: bool
    detail: str = ""


def _groq_client(api_key: str | None = None, timeout: float = 60.0) -> Any:
    key = api_key or os.environ.get(GROQ_API_KEY_ENV)
    if not key:
        raise LlmNotConfigured(f"{GROQ_API_KEY_ENV} is not set.")
    try:
        from groq import Groq
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise LlmNotConfigured("The `groq` package is not installed.") from exc
    return Groq(api_key=key, timeout=timeout)


def list_groq_models(api_key: str | None = None) -> list[str]:
    """Model ids the account can actually use, newest-looking first."""
    client = _groq_client(api_key)
    try:
        listing = client.models.list()
    except Exception as exc:
        raise LlmError(f"Could not list Groq models: {exc}") from exc
    return sorted(item.id for item in getattr(listing, "data", []) or [])


_PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def probe_schema_support(model: str, api_key: str | None = None) -> SchemaProbe:
    """Send one minimal request to find out whether `model` enforces a JSON schema.

    Costs a handful of tokens. Worth it: the alternative is discovering mid-run that the
    configured model 400s on `response_format`, after the corpus has been read and part of
    the classification already paid for.
    """
    client = _groq_client(api_key)
    try:
        client.chat.completions.create(
            model=model,
            # Not a token or two: reasoning models (the gpt-oss family) spend their first
            # output tokens thinking, so a tiny budget returns an empty generation and a
            # `json_validate_failed` that looks exactly like "schema unsupported".
            max_tokens=1024,
            temperature=0.0,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "probe", "schema": _PROBE_SCHEMA, "strict": True},
            },
            messages=[{"role": "user", "content": 'Reply with {"ok": true}.'}],
        )
    except Exception as exc:
        if _is_schema_rejection(exc):
            return SchemaProbe(model, False, "json_object fallback")
        return SchemaProbe(model, False, f"probe failed: {type(exc).__name__}")
    return SchemaProbe(model, True, "strict json_schema")


# --------------------------------------------------------------------------------------
# Test and fallback implementations
# --------------------------------------------------------------------------------------


@dataclass
class FakeLlm:
    """Scripted responses, for tests and `--llm fake`.

    Each call pops the next entry from `responses`. An entry may be a JSON string, a dict
    or a Pydantic model (serialized for you), or an exception instance (raised). Scripting
    failures is the point: P3-T18 needs "fail the verbatim check, then pass it" to be
    expressible.
    """

    provider: str = "fake"
    model: str = "fake-model"
    supports_prompt_cache: bool = False
    responses: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    usage: LlmUsage = field(default_factory=LlmUsage)

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
        reasoning_effort: str | None = None,
    ) -> LlmResponse:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "schema_name": schema_name,
                "max_tokens": max_tokens,
            }
        )
        if not self.responses:
            raise LlmError(
                f"FakeLlm ran out of scripted responses after {len(self.calls)} call(s). "
                f"Add another entry to `responses`."
            )

        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt

        if isinstance(nxt, BaseModel):
            text = nxt.model_dump_json()
        elif isinstance(nxt, str):
            text = nxt
        else:
            text = json.dumps(nxt)

        self.usage.add(input_tokens=0, output_tokens=0)
        return LlmResponse(text=text, model=self.model, schema_enforced=True)


def build_llm(settings: Any, *, override: str | None = None) -> LlmPort:
    """Construct the configured provider.

    `override` comes from `--llm` and wins over the config file, so a developer can force
    `fake` or `none` without editing committed configuration.
    """
    from pulse.config import LlmProvider, require_model

    name = override or settings.pulse.llm.provider.value
    try:
        provider = LlmProvider(name)
    except ValueError as exc:
        valid = ", ".join(p.value for p in LlmProvider)
        raise LlmNotConfigured(f"Unknown LLM provider {name!r}. Valid: {valid}") from exc

    if provider is LlmProvider.NONE:
        return NullLlm()
    if provider is LlmProvider.FAKE:
        # Deliberately unscripted: `fake` exists for tests, which supply their own
        # responses. Reaching a model call through it fails with a clear message rather
        # than fabricating plausible output that could be mistaken for a real pulse.
        return FakeLlm()

    return GroqLlm(
        model=require_model(settings),
        temperature=settings.pulse.llm.temperature,
    )


def usage_for_ledger(llm: LlmPort) -> dict[str, Any]:
    """Snapshot a provider's usage in the shape `models.LlmUsage` expects."""
    usage = getattr(llm, "usage", None)
    if usage is None:
        return {"provider": llm.provider, "model": llm.model}
    return {
        "provider": llm.provider,
        "model": llm.model,
        "calls": usage.calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "estimated_cost_usd": usage.estimated_cost_usd(llm.model),
    }


@dataclass
class NullLlm:
    """Refuses every call. Used when no provider is configured, so the failure is explicit."""

    provider: str = "none"
    model: str = "none"
    supports_prompt_cache: bool = False

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
        reasoning_effort: str | None = None,
    ) -> LlmResponse:
        raise LlmNotConfigured(
            "This stage needs a language model, but no provider is configured. "
            'Set GROQ_API_KEY and `[llm] provider = "groq"` in config/pulse.toml, '
            "or pass --llm fake to run offline."
        )
