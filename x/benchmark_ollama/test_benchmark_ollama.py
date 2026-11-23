import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import openai
import pytest

from x.benchmark_ollama import benchmark_ollama


def test_summary_keeps_exact_context_sizes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    model = "test-model"
    sizes = [4000, 4096, 1_048_576]
    client = Mock(spec=openai.OpenAI)
    monkeypatch.setattr(benchmark_ollama, "check_model_available", lambda *_: True)
    monkeypatch.setattr(benchmark_ollama, "prewarm", lambda *_: 0.0)
    monkeypatch.setattr(benchmark_ollama, "measure_output", lambda *_: [10.0])
    monkeypatch.setattr(benchmark_ollama, "measure_input", lambda _client, _model, size, _limit: [float(size)])
    monkeypatch.setattr(
        benchmark_ollama, "measure_decode_at_context", lambda _client, _model, size, _limit: [size * 2.0]
    )

    def sample_recall(
        _client: openai.OpenAI, model: str, size: int, _count: int, _limit: float
    ) -> list[benchmark_ollama.NiahSample]:
        return [
            benchmark_ollama.NiahSample(
                timestamp="2026-01-01T00:00:00Z",
                model=model,
                context_size_tokens=size,
                depth_frac=0.5,
                needle_code="12345678",
                prompt_char_len=size,
                prompt_tokens=size,
                response_text="12345678" if size == 4096 else "unknown",
                found=size == 4096,
                elapsed_s=1.0,
                num_ctx=benchmark_ollama.num_ctx_for(size),
            )
        ]

    monkeypatch.setattr(benchmark_ollama, "run_niah", sample_recall)
    log_path = tmp_path / "measurements.jsonl"
    result = benchmark_ollama.benchmark_model(client, model, sizes, 5, 1, log_path)
    capsys.readouterr()
    benchmark_ollama.print_summary({model: result}, [model], sizes, 5)

    rows = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert ["output", "10.0", "t/s"] in rows
    assert ["input4k", "4000.0", "t/s"] in rows
    assert ["decode4k", "8000.0", "t/s"] in rows
    assert ["niah4k", "0/1"] in rows
    assert ["input4,096", "4096.0", "t/s"] in rows
    assert ["decode4,096", "8192.0", "t/s"] in rows
    assert ["niah4,096", "1/1"] in rows
    assert ["decode1,048,576", "2097152.0", "t/s"] in rows
    assert [json.loads(line)["context_size_tokens"] for line in log_path.read_text().splitlines()] == sizes


class FakeClock:
    def __init__(self) -> None:
        self.now = 10.0

    def monotonic(self) -> float:
        return self.now

    def chunks(self, timeline: list[tuple[float, str | None]]) -> Iterator[SimpleNamespace]:
        for timestamp, content in timeline:
            self.now = timestamp
            choices = [] if content is None else [SimpleNamespace(delta=SimpleNamespace(content=content))]
            yield SimpleNamespace(choices=choices)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(benchmark_ollama, "time", clock)
    return clock


@pytest.mark.parametrize("use_filled_context", [False, True])
def test_stream_measures_content_chunks_through_deadline(clock: FakeClock, use_filled_context: bool) -> None:
    client = Mock()
    create = client.chat.completions.create
    create.return_value = clock.chunks(
        [(11.0, None), (12.0, "many words"), (13.0, ""), (14.0, "b"), (15.0, "c"), (16.0, "too late")]
    )

    if use_filled_context:
        rates = benchmark_ollama.measure_decode_at_context(client, "test-model", 4096, 5.0)
    else:
        rates = benchmark_ollama.measure_output(client, "test-model", 5.0)

    assert rates == pytest.approx([2.0 / 3.0])
    create.assert_called_once()
    args = create.call_args.kwargs
    assert args["stream"] is True
    assert args["max_tokens"] == 128
    if use_filled_context:
        assert args["extra_body"] == {"options": {"num_ctx": 5222}}
        assert args["messages"][0]["content"].endswith("Now write a short paragraph about the weather.")
    else:
        assert "extra_body" not in args
        assert args["messages"] == [{"role": "user", "content": benchmark_ollama.OUTPUT_SEED_PROMPT}]


def test_streaming_sample_limit_counts_only_measured_rates(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    create = client.chat.completions.create
    monkeypatch.setattr(benchmark_ollama, "MAX_THROUGHPUT_SAMPLES", 2)
    create.side_effect = [
        clock.chunks([(11.0, "one chunk")]),
        clock.chunks([(12.0, "a"), (13.0, "b")]),
        clock.chunks([(14.0, "a"), (16.0, "b")]),
    ]

    assert benchmark_ollama.measure_output(client, "test-model", 100.0) == [1.0, 0.5]
    assert create.call_count == 3


def test_stream_iteration_errors_keep_the_callers_error_policies(
    clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    error = openai.APIError("test error", request=Mock(), body=None)

    def failed_stream() -> Iterator[SimpleNamespace]:
        yield from clock.chunks([(13.0, "a"), (14.0, "b")])
        raise error

    client = Mock()
    create = client.chat.completions.create
    create.side_effect = [clock.chunks([(11.0, "a"), (12.0, "b")]), failed_stream()]
    assert benchmark_ollama.measure_decode_at_context(client, "test-model", 4096, 100.0) == [1.0]
    assert "test error" in capsys.readouterr().out

    create.side_effect = [failed_stream()]
    with pytest.raises(openai.APIError) as raised:
        benchmark_ollama.measure_output(client, "test-model", 100.0)
    assert raised.value is error
