import nltk
import pytest

from util.bazel.runfiles import get_required_path


def _refuse_download(*_args: object, **_kwargs: object) -> bool:
    raise RuntimeError("NLTK data must come from the Bazel-fetched repos, not a download")


def pytest_configure(config: pytest.Config) -> None:
    # The Bazel-fetched NLTK data is the whole search path, replacing NLTK's defaults: a corpus left
    # in `~/nltk_data` on a reused worker must not stand in for the pinned one.
    nltk.data.path[:] = [
        str(get_required_path("nltk_words/corpora/words").parents[1]),
        str(get_required_path("nltk_tagger/taggers/averaged_perceptron_tagger_eng").parents[1]),
    ]
    # `wordle_env` downloads any corpus it cannot find, which a worker with network access would
    # satisfy silently; refuse, so a path mistake fails the test.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(nltk, "download", _refuse_download)
    config.add_cleanup(monkeypatch.undo)
