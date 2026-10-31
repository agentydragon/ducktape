import pytest_bazel

from x.ai_usage_investigation.langfuse_usage_extract import normalize_row, resolve_provider


def usage_row(raw_usage: dict[str, int], **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "raw_usage_sums": raw_usage,
        "raw_provided_usage_sums": {},
        "input_known_requests": 1,
        "cache_read_known_requests": 1,
        "cache_write_known_requests": 1,
        "output_known_requests": 1,
        "reasoning_known_requests": 1,
        "total_known_requests": 1,
        "input_positive_cache_requests": 1,
        "input_total_inclusive_requests": 0,
        "input_total_exclusive_requests": 0,
        "input_total_algebra_unknown_requests": 0,
        "input_cache_unreported_requests": 0,
        "reasoning_positive_requests": 0,
        "reasoning_total_additive_requests": 0,
        "reasoning_total_subset_requests": 0,
        "reasoning_total_algebra_unknown_requests": 0,
        "gen_ai_input_attr_sum": 0,
        "gen_ai_output_attr_sum": 0,
        "gen_ai_total_attr_sum": 0,
        "gen_ai_cache_read_attr_sum": 0,
        "gen_ai_cache_write_attr_sum": 0,
        "llm_prompt_attr_sum": 0,
        "llm_completion_attr_sum": 0,
        "llm_reasoning_attr_sum": 0,
        "llm_cache_read_attr_sum": 0,
        "no_explicit_id_requests": 1,
        "distinct_provider_response_ids": 0,
        "distinct_litellm_call_ids_without_response_id": 0,
        "span_events_before_id_dedup": 1,
        "provider": "unknown",
        "payment_lane": "unknown_route_scope",
        "model": "gemini-3.5-flash-lite",
    }
    row.update(overrides)
    return row


def test_input_cache_inclusivity_uses_per_call_total_algebra() -> None:
    inclusive = usage_row(
        {"input": 100, "input_cached_tokens": 40, "output": 20, "total": 120}, input_total_inclusive_requests=1
    )
    exclusive = usage_row(
        {"input": 100, "input_cached_tokens": 40, "output": 20, "total": 160}, input_total_exclusive_requests=1
    )

    assert normalize_row(inclusive)["uncached_input_tokens"] == 60
    assert normalize_row(exclusive)["uncached_input_tokens"] == 100


def test_reasoning_subset_is_not_added_to_output() -> None:
    row = usage_row(
        {"input": 100, "input_cached_tokens": 40, "output": 20, "completion_details.reasoning": 10, "total": 120},
        input_total_inclusive_requests=1,
        reasoning_positive_requests=1,
        reasoning_total_subset_requests=1,
    )

    normalized = normalize_row(row)

    assert normalized["output_tokens"] == 20
    assert normalized["reasoning_output_tokens"] == 10
    assert normalized["total_reconciliation_delta"] == 0


def test_transport_provider_does_not_override_model_family() -> None:
    assert resolve_provider(
        {"model": "gemini-3.5-flash-lite", "payment_lane": "unknown_route_scope", "provider_observed": "anthropic"}
    ) == ("unknown", "manufacturer_unresolved_provider_observed_not_authoritative")


if __name__ == "__main__":
    pytest_bazel.main()
