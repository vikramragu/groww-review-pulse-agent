"""P2-T9: classification, entirely offline via `FakeLlm`.

The reconciliation tests are the important ones. A model asked to echo 40 indices will
sometimes omit one, repeat one, or invent one — and pairing its results with the input list
positionally would mislabel reviews *silently*, which is the same family of failure as a
paraphrased quote. Every one of those cases is asserted here, including that the bad label
becomes `None` rather than something plausible-looking.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pulse.analysis.classify import (
    MAX_BATCH_ATTEMPTS,
    ClassificationBatch,
    ClassificationCache,
    ClassifyReport,
    classify_reviews,
)
from pulse.config import Settings, load_settings
from pulse.llm import (
    FakeLlm,
    LlmError,
    LlmNotConfigured,
    NullLlm,
    build_llm,
    json_schema_for,
)
from pulse.models import Review, Source, Theme


@pytest.fixture
def settings() -> Settings:
    return load_settings(Path("config"))


def a_review(index: int, text: str = "Withdrawal is stuck again and again.") -> Review:
    return Review(
        review_id=f"{index:016x}",
        source=Source.PLAY_STORE,
        rating=2,
        text=text,
        submitted_on="2026-10-07",  # type: ignore[arg-type]
        iso_week="2026-W41",
        scrubbed=True,
    )


def corpus(size: int) -> list[Review]:
    return [a_review(index) for index in range(size)]


def reply(*items: tuple[int, str | None, float]) -> dict[str, object]:
    return {
        "results": [
            {"index": index, "theme": theme, "confidence": confidence}
            for index, theme, confidence in items
        ]
    }


def full_reply(size: int, theme: str = "payments_withdrawals") -> dict[str, object]:
    return reply(*[(i, theme, 0.9) for i in range(1, size + 1)])


def no_cache(settings: Settings, tmp_path: Path) -> ClassificationCache:
    return ClassificationCache(
        tmp_path / "classify.json",
        taxonomy_version=settings.taxonomy_version,
        model="fake-model",
    )


# --------------------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------------------


def test_classifies_every_review(settings: Settings, tmp_path: Path) -> None:
    reviews = corpus(3)
    llm = FakeLlm(responses=[full_reply(3)])

    classified, report = classify_reviews(
        reviews, llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert [c.review_id for c in classified] == [r.review_id for r in reviews]
    assert all(c.theme is Theme.PAYMENTS_WITHDRAWALS for c in classified)
    assert report.api_calls == 1
    assert report.unclassified == 0


def test_output_order_matches_input_order(settings: Settings, tmp_path: Path) -> None:
    """Byte-stable artifacts are what make the Phase 6 golden test possible."""
    reviews = corpus(5)
    llm = FakeLlm(
        responses=[
            reply(
                (5, "app_reliability", 0.9),
                (1, "onboarding_kyc", 0.9),
                (3, "charges_support", 0.9),
                (2, "orders_execution", 0.9),
                (4, "payments_withdrawals", 0.9),
            )
        ]
    )

    classified, _ = classify_reviews(
        reviews, llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert [c.review_id for c in classified] == [r.review_id for r in reviews]
    assert classified[0].theme is Theme.ONBOARDING_KYC
    assert classified[4].theme is Theme.APP_RELIABILITY


def test_an_empty_corpus_makes_no_call(settings: Settings, tmp_path: Path) -> None:
    llm = FakeLlm(responses=[])
    classified, report = classify_reviews(
        [], llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert classified == []
    assert report == ClassifyReport(reviews_in=0)
    assert llm.calls == []


def test_theme_counts_are_reported(settings: Settings, tmp_path: Path) -> None:
    llm = FakeLlm(
        responses=[
            reply(
                (1, "app_reliability", 0.9),
                (2, "app_reliability", 0.9),
                (3, "charges_support", 0.9),
            )
        ]
    )
    _, report = classify_reviews(
        corpus(3), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert report.theme_counts == {"app_reliability": 2, "charges_support": 1}


# --------------------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------------------


def test_the_corpus_is_split_into_batches(settings: Settings, tmp_path: Path) -> None:
    tuned = settings.model_copy(
        update={
            "pulse": settings.pulse.model_copy(
                update={"llm": settings.pulse.llm.model_copy(update={"batch_size": 2})}
            )
        }
    )
    llm = FakeLlm(responses=[full_reply(2), full_reply(2), full_reply(1)])

    _, report = classify_reviews(
        corpus(5), llm=llm, settings=tuned, cache=no_cache(tuned, tmp_path)
    )

    assert report.batches == 3
    assert report.api_calls == 3


def test_each_batch_is_indexed_from_one(settings: Settings, tmp_path: Path) -> None:
    """Indices are per batch, not global — reconciliation depends on it."""
    tuned = settings.model_copy(
        update={
            "pulse": settings.pulse.model_copy(
                update={"llm": settings.pulse.llm.model_copy(update={"batch_size": 2})}
            )
        }
    )
    llm = FakeLlm(responses=[full_reply(2), full_reply(2)])

    classify_reviews(corpus(4), llm=llm, settings=tuned, cache=no_cache(tuned, tmp_path))

    for call in llm.calls:
        assert call["user"].startswith("[1] ")  # type: ignore[union-attr]


def test_the_system_prompt_is_identical_across_batches(settings: Settings, tmp_path: Path) -> None:
    """A varying system prefix would defeat prompt caching and make runs incomparable."""
    tuned = settings.model_copy(
        update={
            "pulse": settings.pulse.model_copy(
                update={"llm": settings.pulse.llm.model_copy(update={"batch_size": 1})}
            )
        }
    )
    llm = FakeLlm(responses=[full_reply(1), full_reply(1), full_reply(1)])

    classify_reviews(corpus(3), llm=llm, settings=tuned, cache=no_cache(tuned, tmp_path))

    systems = {call["system"] for call in llm.calls}
    assert len(systems) == 1


# --------------------------------------------------------------------------------------
# Reconciliation — the model's indices are not trusted
# --------------------------------------------------------------------------------------


def test_a_missing_index_becomes_unclassified(settings: Settings, tmp_path: Path) -> None:
    """The model omitted review 2. It must not inherit review 3's label."""
    llm = FakeLlm(responses=[reply((1, "app_reliability", 0.9), (3, "charges_support", 0.9))])

    classified, report = classify_reviews(
        corpus(3), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert classified[0].theme is Theme.APP_RELIABILITY
    assert classified[1].theme is None
    assert classified[2].theme is Theme.CHARGES_SUPPORT
    assert report.missing_from_response == 1
    assert report.unclassified == 1


def test_an_out_of_range_index_is_discarded(settings: Settings, tmp_path: Path) -> None:
    llm = FakeLlm(responses=[reply((1, "app_reliability", 0.9), (99, "charges_support", 0.9))])

    classified, report = classify_reviews(
        corpus(2), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert report.spurious_indices == 1
    assert classified[1].theme is None


def test_a_duplicated_index_keeps_the_first(settings: Settings, tmp_path: Path) -> None:
    llm = FakeLlm(responses=[reply((1, "app_reliability", 0.9), (1, "charges_support", 0.9))])

    classified, report = classify_reviews(
        corpus(1), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert report.duplicate_indices == 1
    assert classified[0].theme is Theme.APP_RELIABILITY


def test_an_empty_result_list_leaves_everything_unclassified(
    settings: Settings, tmp_path: Path
) -> None:
    llm = FakeLlm(responses=[{"results": []}])

    classified, report = classify_reviews(
        corpus(3), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert all(c.theme is None for c in classified)
    assert report.unclassified == 3


def test_an_invented_theme_is_rejected_by_the_schema(settings: Settings, tmp_path: Path) -> None:
    """The closed enum is the ≤5 constraint; an invented theme must not pass through.

    Two attempts both return a bogus theme, so the batch ends unclassified rather than
    inventing a sixth theme.
    """
    bogus = reply((1, "customer_delight", 0.9))
    llm = FakeLlm(responses=[bogus, bogus])

    classified, report = classify_reviews(
        corpus(1), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert classified[0].theme is None
    assert report.batches_failed == 1
    assert report.batch_retries == 1


# --------------------------------------------------------------------------------------
# Confidence floor
# --------------------------------------------------------------------------------------


def test_low_confidence_labels_are_discarded(settings: Settings, tmp_path: Path) -> None:
    """Below the floor, an honest "don't know" beats a confident guess."""
    llm = FakeLlm(responses=[reply((1, "app_reliability", 0.2))])

    classified, report = classify_reviews(
        corpus(1), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert classified[0].theme is None
    assert report.low_confidence == 1


def test_the_models_own_confidence_is_preserved_when_discarded(
    settings: Settings, tmp_path: Path
) -> None:
    """Keeping the number makes the discard auditable after the fact."""
    llm = FakeLlm(responses=[reply((1, "app_reliability", 0.21))])

    classified, _ = classify_reviews(
        corpus(1), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert classified[0].theme is None
    assert classified[0].confidence == pytest.approx(0.21)


def test_confidence_at_the_floor_is_kept(settings: Settings, tmp_path: Path) -> None:
    floor = settings.pulse.thresholds.min_confidence
    llm = FakeLlm(responses=[reply((1, "app_reliability", floor))])

    classified, _ = classify_reviews(
        corpus(1), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert classified[0].theme is Theme.APP_RELIABILITY


def test_an_explicit_null_theme_is_not_counted_as_low_confidence(
    settings: Settings, tmp_path: Path
) -> None:
    """The model declining to classify is different from us overriding it."""
    llm = FakeLlm(responses=[reply((1, None, 0.1))])

    classified, report = classify_reviews(
        corpus(1), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert classified[0].theme is None
    assert report.low_confidence == 0
    assert report.unclassified == 1


# --------------------------------------------------------------------------------------
# Retry on invalid output
# --------------------------------------------------------------------------------------


def test_a_malformed_reply_is_retried_once_then_succeeds(
    settings: Settings, tmp_path: Path
) -> None:
    llm = FakeLlm(responses=["this is not json at all", full_reply(1)])

    classified, report = classify_reviews(
        corpus(1), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert classified[0].theme is Theme.PAYMENTS_WITHDRAWALS
    assert report.batch_retries == 1
    assert report.api_calls == 2
    assert report.batches_failed == 0


def test_the_retry_names_the_specific_failure(settings: Settings, tmp_path: Path) -> None:
    """ "Try again" produces a differently-wrong answer; naming the fault does not."""
    llm = FakeLlm(responses=["not json", full_reply(1)])

    classify_reviews(corpus(1), llm=llm, settings=settings, cache=no_cache(settings, tmp_path))

    retry_prompt = llm.calls[1]["user"]
    assert "was rejected" in retry_prompt  # type: ignore[operator]
    assert "JSON" in retry_prompt  # type: ignore[operator]


def test_two_failures_leave_the_batch_unclassified(settings: Settings, tmp_path: Path) -> None:
    llm = FakeLlm(responses=["not json", "still not json"])

    classified, report = classify_reviews(
        corpus(2), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert all(c.theme is None for c in classified)
    assert report.batches_failed == 1
    assert report.api_calls == MAX_BATCH_ATTEMPTS


def test_json_wrapped_in_a_markdown_fence_is_accepted(settings: Settings, tmp_path: Path) -> None:
    """A model that returned valid JSON in a fence did the useful part of the job."""
    fenced = "```json\n" + json.dumps(full_reply(1)) + "\n```"
    llm = FakeLlm(responses=[fenced])

    classified, report = classify_reviews(
        corpus(1), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert classified[0].theme is Theme.PAYMENTS_WITHDRAWALS
    assert report.batch_retries == 0


def test_a_transport_failure_is_not_downgraded_to_unclassified(
    settings: Settings, tmp_path: Path
) -> None:
    """A rate limit or auth error must abort, not silently produce an unlabelled corpus.

    Treating it as "unclassified" would publish a pulse built on reviews the model never
    actually read, with nothing in the output to say so.
    """
    llm = FakeLlm(responses=[LlmError("429 rate limited")])

    with pytest.raises(LlmError, match="429"):
        classify_reviews(corpus(1), llm=llm, settings=settings, cache=no_cache(settings, tmp_path))


# --------------------------------------------------------------------------------------
# The on-disk cache
# --------------------------------------------------------------------------------------


def test_a_second_run_makes_no_api_call(settings: Settings, tmp_path: Path) -> None:
    """The property that makes weekly re-runs nearly free (architecture.md §8.1)."""
    cache_path = tmp_path / "classify.json"
    reviews = corpus(3)

    first = FakeLlm(responses=[full_reply(3)])
    classify_reviews(
        reviews,
        llm=first,
        settings=settings,
        cache=ClassificationCache(
            cache_path, taxonomy_version=settings.taxonomy_version, model="fake-model"
        ),
    )

    second = FakeLlm(responses=[])
    classified, report = classify_reviews(
        reviews,
        llm=second,
        settings=settings,
        cache=ClassificationCache(
            cache_path, taxonomy_version=settings.taxonomy_version, model="fake-model"
        ),
    )

    assert second.calls == []
    assert report.api_calls == 0
    assert report.from_cache == 3
    assert report.cache_hit_rate == 1.0
    assert all(c.theme is Theme.PAYMENTS_WITHDRAWALS for c in classified)


def test_only_uncached_reviews_are_sent(settings: Settings, tmp_path: Path) -> None:
    cache_path = tmp_path / "classify.json"
    cache = ClassificationCache(
        cache_path, taxonomy_version=settings.taxonomy_version, model="fake-model"
    )
    classify_reviews(
        corpus(2), llm=FakeLlm(responses=[full_reply(2)]), settings=settings, cache=cache
    )

    grown = corpus(4)
    llm = FakeLlm(responses=[full_reply(2, "app_reliability")])
    classified, report = classify_reviews(
        grown,
        llm=llm,
        settings=settings,
        cache=ClassificationCache(
            cache_path, taxonomy_version=settings.taxonomy_version, model="fake-model"
        ),
    )

    assert report.from_cache == 2
    assert len(llm.calls) == 1
    # The two new reviews were renumbered 1-2 within their own batch.
    assert llm.calls[0]["user"].startswith("[1] ")  # type: ignore[union-attr]
    assert classified[0].theme is Theme.PAYMENTS_WITHDRAWALS
    assert classified[2].theme is Theme.APP_RELIABILITY


def test_a_taxonomy_version_change_invalidates_the_cache(
    settings: Settings, tmp_path: Path
) -> None:
    """Labels from a taxonomy that no longer exists are worse than no labels."""
    cache_path = tmp_path / "classify.json"
    classify_reviews(
        corpus(1),
        llm=FakeLlm(responses=[full_reply(1)]),
        settings=settings,
        cache=ClassificationCache(cache_path, taxonomy_version="1", model="fake-model"),
    )

    llm = FakeLlm(responses=[full_reply(1)])
    _, report = classify_reviews(
        corpus(1),
        llm=llm,
        settings=settings,
        cache=ClassificationCache(cache_path, taxonomy_version="2", model="fake-model"),
    )

    assert report.from_cache == 0
    assert report.api_calls == 1


def test_a_model_change_invalidates_the_cache(settings: Settings, tmp_path: Path) -> None:
    """Addition to the plan's cache key, and it matters for O5.

    Serving labels produced by one model while a run is otherwise using another would give
    the corpus two different judges — which would quietly undermine the model comparison
    O5 exists to make.
    """
    cache_path = tmp_path / "classify.json"
    classify_reviews(
        corpus(1),
        llm=FakeLlm(responses=[full_reply(1)]),
        settings=settings,
        cache=ClassificationCache(
            cache_path, taxonomy_version=settings.taxonomy_version, model="model-a"
        ),
    )

    llm = FakeLlm(responses=[full_reply(1)])
    _, report = classify_reviews(
        corpus(1),
        llm=llm,
        settings=settings,
        cache=ClassificationCache(
            cache_path, taxonomy_version=settings.taxonomy_version, model="model-b"
        ),
    )

    assert report.from_cache == 0
    assert report.api_calls == 1


def test_a_disabled_cache_always_calls(settings: Settings, tmp_path: Path) -> None:
    cache_path = tmp_path / "classify.json"
    for _ in range(2):
        llm = FakeLlm(responses=[full_reply(1)])
        _, report = classify_reviews(
            corpus(1),
            llm=llm,
            settings=settings,
            cache=ClassificationCache(
                cache_path,
                taxonomy_version=settings.taxonomy_version,
                model="fake-model",
                enabled=False,
            ),
        )
        assert report.api_calls == 1
        assert report.from_cache == 0
    assert not cache_path.exists()


def test_a_corrupt_cache_is_ignored_not_fatal(settings: Settings, tmp_path: Path) -> None:
    """A cache problem is a performance problem, not a reason to fail a run."""
    cache_path = tmp_path / "classify.json"
    cache_path.write_text("{not json", encoding="utf-8")

    llm = FakeLlm(responses=[full_reply(1)])
    _, report = classify_reviews(
        corpus(1),
        llm=llm,
        settings=settings,
        cache=ClassificationCache(
            cache_path, taxonomy_version=settings.taxonomy_version, model="fake-model"
        ),
    )

    assert report.api_calls == 1


def test_the_cache_file_contains_no_review_text(settings: Settings, tmp_path: Path) -> None:
    """The cache stores labels keyed by id, never the text that produced them."""
    cache_path = tmp_path / "classify.json"
    text = "Mail me at notareal@example.com, my transfer never arrived."
    classify_reviews(
        [a_review(0, text)],
        llm=FakeLlm(responses=[full_reply(1)]),
        settings=settings,
        cache=ClassificationCache(
            cache_path, taxonomy_version=settings.taxonomy_version, model="fake-model"
        ),
    )

    written = cache_path.read_text(encoding="utf-8")
    assert "notareal" not in written
    # Any distinctive run of words from the review, rather than a single word that could
    # legitimately collide with a theme name ("withdrawal" is in payments_withdrawals).
    assert "never arrived" not in written
    assert text not in written
    assert set(json.loads(written)["entries"]["0000000000000000"]) == {
        "theme",
        "confidence",
    }


# --------------------------------------------------------------------------------------
# Report shape
# --------------------------------------------------------------------------------------


def test_report_summary_has_no_review_text(settings: Settings, tmp_path: Path) -> None:
    llm = FakeLlm(responses=[full_reply(1)])
    _, report = classify_reviews(
        [a_review(0, "Mail notareal@example.com now.")],
        llm=llm,
        settings=settings,
        cache=no_cache(settings, tmp_path),
    )

    assert "notareal" not in str(report.summary())


def test_report_records_that_the_provider_has_no_prompt_cache(
    settings: Settings, tmp_path: Path
) -> None:
    """Groq has no cache breakpoint; the ledger should say so rather than imply a miss."""
    llm = FakeLlm(responses=[full_reply(1)])
    _, report = classify_reviews(
        corpus(1), llm=llm, settings=settings, cache=no_cache(settings, tmp_path)
    )

    assert report.prompt_cache_supported is False
    assert report.summary()["prompt_cache_reads"] == 0


# --------------------------------------------------------------------------------------
# Provider factory
# --------------------------------------------------------------------------------------


def test_build_llm_honours_the_override(settings: Settings) -> None:
    assert isinstance(build_llm(settings, override="none"), NullLlm)
    assert isinstance(build_llm(settings, override="fake"), FakeLlm)


def test_build_llm_rejects_an_unknown_provider(settings: Settings) -> None:
    with pytest.raises(LlmNotConfigured, match="Unknown LLM provider"):
        build_llm(settings, override="openai")


def test_groq_without_a_model_fails_with_a_pointer(settings: Settings) -> None:
    """An unchosen model must say how to choose one, not fail at request time.

    The model is cleared explicitly rather than relying on the committed config being
    empty -- that coupling broke the moment a model was actually chosen.
    """
    from pulse.config import ConfigError

    unset = settings.model_copy(
        update={
            "pulse": settings.pulse.model_copy(
                update={"llm": settings.pulse.llm.model_copy(update={"model": ""})}
            )
        }
    )
    with pytest.raises(ConfigError, match="pulse models"):
        build_llm(unset, override="groq")


def test_the_null_provider_refuses_clearly(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(LlmNotConfigured, match="no provider is configured"):
        classify_reviews(
            corpus(1),
            llm=NullLlm(),
            settings=settings,
            cache=no_cache(settings, tmp_path),
        )


def test_the_fake_provider_runs_out_of_script_loudly(settings: Settings, tmp_path: Path) -> None:
    """`fake` is for tests. Reaching a real call through it must not fabricate output."""
    with pytest.raises(LlmError, match="ran out of scripted responses"):
        classify_reviews(
            corpus(1),
            llm=FakeLlm(responses=[]),
            settings=settings,
            cache=no_cache(settings, tmp_path),
        )


# --------------------------------------------------------------------------------------
# Strict-schema reshaping
#
# Pydantic's default JSON Schema is rejected by strict structured-output modes, and the
# resulting 400 reads like "this model does not support schemas". That misattribution is
# the dangerous part: the adaptive fallback would then disable enforcement for the whole
# run because of what is actually a fixable schema shape.
# --------------------------------------------------------------------------------------


def test_the_schema_has_no_refs_or_defs() -> None:
    schema = json_schema_for(ClassificationBatch)
    assert "$defs" not in schema
    assert "$ref" not in json.dumps(schema)


def test_the_nullable_enum_is_a_single_enum_with_null() -> None:
    """`Theme | None` must not become an undiscriminated `anyOf`."""
    schema = json_schema_for(ClassificationBatch)
    theme = schema["properties"]["results"]["items"]["properties"]["theme"]

    assert "anyOf" not in theme
    assert set(theme["enum"]) == {t.value for t in Theme} | {None}
    # `type` is dropped: "string" alongside a null member is a contradiction.
    assert "type" not in theme


def test_every_object_forbids_extra_properties() -> None:
    schema = json_schema_for(ClassificationBatch)

    def check(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for item in node:
                check(item)

    check(schema)


def test_the_five_themes_are_still_the_only_allowed_values() -> None:
    """The reshaping must not loosen the ≤5 constraint it exists to carry."""
    schema = json_schema_for(ClassificationBatch)
    theme = schema["properties"]["results"]["items"]["properties"]["theme"]
    assert len([v for v in theme["enum"] if v is not None]) == 5


def test_the_schema_is_json_serializable() -> None:
    """It is sent over the wire, so anything non-serializable fails at request time."""
    json.dumps(json_schema_for(ClassificationBatch))
