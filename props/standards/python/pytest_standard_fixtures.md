---
title: Use pytest's standard fixtures for temp dirs and monkeypatching
kind: outcome
---

Pytest-based tests use standard built-ins for temporary paths and patching instead of hand-rolling.
Use `tmp_path` (or `tmp_path_factory` for broader scope), not raw `tempfile`/manual cleanup.
Use `monkeypatch` for environment, cwd and sys path changes.

## Acceptance criteria (checklist)

- Temporary filesystem:
  - Use `tmp_path` for per-test temporary directories; construct paths with `/` and `Path` APIs
  - Use `tmp_path_factory` for module/session-scoped directories when needed
  - Do not use raw `tempfile.mkdtemp/NamedTemporaryFile` unless code under test specifically requires it (document why `tmp_path` cannot be used)
- Process state:
  - Use `monkeypatch.chdir(tmp_path)` to set working directory (no hand-rolled cwd context managers)
  - Use `monkeypatch.setenv/monkeypatch.delenv` for environment variables (no direct `os.environ[...] = ...` in tests)
  - Use `monkeypatch.syspath_prepend` for import path tweaks
- Patching: use `monkeypatch`, `unittest.mock.patch` or other library of choice - **not** hand-rolled patching.
  - `monkeypatch.setattr` or `unittest.mock.patch` for object/function patching — no manual save/restore
  - `monkeypatch.setitem` or `unittest.mock.patch.dict` for mapping patching
- Use `tmp_path`, not legacy `tmpdir` unless testing code strictly requiring `py.path` (document why).

## Forbidden

- Hand-rolled cwd managers/context managers; use `monkeypatch.chdir(tmp_path)`
- Home-grown temp dir helpers or manual `tempfile` + cleanup; use `tmp_path`/`tmp_path_factory` or document why that would not work
- Home-rolled env mutation; use `monkeypatch.setenv` or `unittest.mock.patch.dict`

## Positive examples

```python
# tmp_path for per-test temp dirs
def test_writes_file(tmp_path: Path):
    out = tmp_path / "data.txt"
    out.write_text("hello")
    assert out.read_text() == "hello"

# tmp_path_factory for broader scope
@pytest.fixture(scope="module")
def module_tmp(module_tmp_path_factory):
    p = module_tmp_path_factory.mktemp("dataset")
    ...

# monkeypatch for cwd and env
def test_runs_in_temp_dir(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_MODE", "test")
    run_main()
    assert (tmp_path / "output.log").exists()

# monkeypatch.setattr without manual save/restore
def test_disables_network(monkeypatch):
    monkeypatch.setattr("mymodule.http_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")))
    with pytest.raises(RuntimeError):
        mymodule.fetch()
```

## Negative examples

Hand-rolled cwd manager — do not do this, use `monkeypatch.chdir`:

```python
class Cwd:
    def __init__(self, path):
        self.path = path
        self.prev = None
    def __enter__(self):
        self.prev = os.getcwd(); os.chdir(self.path)
    def __exit__(self, *_):
        os.chdir(self.prev)

def test_runs_in_temp_dir(...):
    with Cwd(tempfile.mkdtemp()):
        ...
```

Direct env mutation (with incorrect cleanup) where `monkeypatch` would work:

```python
def test_runs_in_temp_dir(...):
    os.environ["APP_MODE"] = "test"
    run_main()
    del os.environ["APP_MODE"]
```

Do not use hand-rolled tempfiles where `tmp_path` would work:

```python
def test_writes_file():
    root = Path(tempfile.mkdtemp())
    try:
        ...
    finally:
        shutil.rmtree(root)
```

## Exceptions

- When testing code that explicitly consumes `py.path` objects, `tmpdir` can be used.
  Prefer migrating the code under test to `pathlib.Path` and `tmp_path` when feasible.
- If third-party API requires raw `tempfile` handles (e.g., needs a real OS-level fd), document the reason and keep the scope minimal

## See also

- [PathLike (Python)](./pathlike.md)
- [Pathlib usage (Python)](./pathlib.md)
