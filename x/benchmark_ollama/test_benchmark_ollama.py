import json
from pathlib import Path
from unittest.mock import Mock

import openai
import pytest
import pytest_bazel

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


if __name__ == "__main__":
    pytest_bazel.main()
