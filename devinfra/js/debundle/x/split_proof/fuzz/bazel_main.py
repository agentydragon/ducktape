"""Run the render differential fuzzer with tools declared in Bazel."""

import os
import runpy
from pathlib import Path

from python.runfiles import Runfiles


def main() -> None:
    runfiles = Runfiles.Create()
    if runfiles is None:
        raise RuntimeError("Bazel runfiles are unavailable")
    for tool in ("NODE", "RENDER"):
        runfile = os.environ[f"{tool}_RUNFILE"]
        resolved = runfiles.Rlocation(runfile)
        if not resolved or not Path(resolved).is_file():
            raise FileNotFoundError(runfile)
        os.environ[tool] = str(Path(resolved).absolute())
    runpy.run_path(str(Path(__file__).with_name("fuzz.py")), run_name="__main__")


if __name__ == "__main__":
    main()
