"""The Groq client survives the free tier's per-minute token limit on a cold cache."""

from __future__ import annotations

from pulse.llm import GROQ_MAX_RETRIES, GroqLlm


def test_the_client_retries_rate_limits_more_than_the_sdk_default() -> None:
    llm = GroqLlm("openai/gpt-oss-120b", api_key="test-key-not-real")
    assert llm._client.max_retries == GROQ_MAX_RETRIES > 2
