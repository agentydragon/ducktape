---
title: Pass Path objects to PathLike APIs (no str())
kind: outcome
---

Agent-edited code does not cast `pathlib.Path` (or other PathLike) to `str` when calling APIs that accept path-like objects; it passes the `Path` directly.

## Acceptance criteria (checklist)

- No `str(path)` when the target API accepts `os.PathLike`
- `Path` (or any `os.PathLike`) is passed directly to the API
- This applies across subprocess program/args, filesystem APIs, and archive/logging constructors commonly used in Python 3.8+

## Common APIs that accept PathLike (non-exhaustive)

- subprocess: `Popen`, `run`, `check_call`, `check_output` (program, arguments, and `env` mapping values)
- builtin/open: `open(path)`
- os: `stat`, `listdir`, many file ops via `os.fspath`
- shutil: `copy`, `copyfile`, `rmtree`, etc.
- zipfile/tarfile: `zipfile.ZipFile(path)`, `tarfile.open(path)`
- logging: `logging.FileHandler(path)`

## Positive examples

```python
from pathlib import Path
import subprocess
import logging

cfg = Path("/etc/tool/config.ini")
log = Path("/var/log/tool.log")
subprocess.run([Path("/usr/bin/tool"), cfg], check=True, env={"FOO": Path("/tmp/x")})
fh = logging.FileHandler(log)
```

```python
from pathlib import Path
import shutil

src = Path("data/input.bin")
dst = Path("data/out/input.bin")
shutil.copy(src, dst)
```

```python
from pathlib import Path
import zipfile

archive = Path("build/artifacts.zip")
with zipfile.ZipFile(archive, "w") as zf:
    zf.write(Path("build/report.json"))
```

## Negative examples

```python
# Casting Path to str for subprocess — forbidden
cfg = Path("/etc/tool/config.ini")
subprocess.run(["/usr/bin/tool", str(cfg)])
```

```python
# Casting Path to str for shutil — forbidden
src = Path("data/input.bin"); dst = Path("data/out/input.bin")
shutil.copy(str(src), str(dst))
```

```python
# Casting Path to str for FileHandler — forbidden
log = Path("/var/log/tool.log")
fh = logging.FileHandler(str(log))
```

```python
# Casting Path to str for subprocess env mapping — forbidden
subprocess.run(["/usr/bin/env"], env={"FOO": str(Path("/tmp/x"))})
```

```python
# Legacy API caveat: some stdlib still requires str
# Example: glob.glob requires a string pattern, not Path
import glob
pattern = Path("data") / "*.csv"
files = glob.glob(str(pattern))
```
