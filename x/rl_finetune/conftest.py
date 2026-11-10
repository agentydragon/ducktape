import nltk
import pytest
from python.runfiles import Runfiles


def _refuse_download(*_args: object, **_kwargs: object) -> bool:
    raise RuntimeError("NLTK data must come from the Bazel-fetched repos, not a download")


def pytest_configure(config: pytest.Config) -> None:
    # The Bazel-fetched NLTK data is the whole search path, replacing NLTK's defaults: a corpus left
    # in `~/nltk_data` on a reused worker must not stand in for the pinned one.
    runfiles_root = Runfiles.CreateOrRaise().root()
    nltk.data.path[:] = [
        str((runfiles_root / "nltk_words/corpora").resolve().parent),
        str((runfiles_root / "nltk_tagger/taggers").resolve().parent),
    ]
    # `wordle_env` downloads any corpus it cannot find, which a worker with network access would
    # satisfy silently; refuse, so a path mistake fails the test.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(nltk, "download", _refuse_download)
    config.add_cleanup(monkeypatch.undo)
