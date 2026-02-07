# Nix + Python + NumPy/Pandas Integration Issues

## Problem Summary

When using Nix-managed Python with virtual environments (venv) and installing NumPy/Pandas via pip/uv, the packages fail to import due to missing shared libraries. This is a common issue when mixing Nix's Python with PyPI binary wheels.

## Environment Details

- **Python**: 3.12.11 (from Nix profile)
- **Package Manager**: uv (for fast pip-compatible installs)
- **Virtual Environment**: `.venv` managed by direnv
- **Problematic Packages**: numpy, pandas (and any package with compiled C extensions)

## Specific Error Messages Encountered

### Error 1: Missing libstdc++.so.6

```
ImportError: libstdc++.so.6: cannot open shared object file: No such file or directory
```

### Error 2: Missing libz.so.1

```
ImportError: libz.so.1: cannot open shared object file: No such file or directory
```

### Error 3: Generic NumPy Import Failure

```
ImportError:

IMPORTANT: PLEASE READ THIS FOR ADVICE ON HOW TO SOLVE THIS ISSUE!

Importing the numpy C-extensions failed. This error can happen for
many reasons, often due to issues with your setup or how NumPy was
installed.

We have compiled some common reasons and troubleshooting tips at:

    https://numpy.org/devdocs/user/troubleshooting-importerror.html

Please note and check the following:

  * The Python version is: Python3.12 from "/home/agentydragon/code/ducktape/.venv/bin/python"
  * The NumPy version is: "2.3.2"

and make sure that they are the versions you expect.
Please carefully study the documentation linked above for further help.

Original error was: libstdc++.so.6: cannot open shared object file: No such file or directory
```

### Error 4: Misleading "Source Directory" Error

```
ImportError: Error importing numpy: you should not try to import numpy from
        its source directory; please exit the numpy source tree, and relaunch
        your python interpreter from there.
```

(This error is misleading - it's actually a library linking issue, not a source directory issue)

## Root Cause

PyPI wheels are compiled against standard Linux library locations (`/usr/lib`, `/lib`), but Nix stores libraries in `/nix/store/...`. When pip/uv installs these wheels, they can't find the required shared libraries at runtime.

## Solutions Attempted

### 1. ❌ Setting LD_LIBRARY_PATH manually

```bash
export LD_LIBRARY_PATH="/nix/store/.../lib:$LD_LIBRARY_PATH"
```

**Result**: Partial success, but fragile and requires finding exact Nix store paths

### 2. ❌ Installing numpy from source

```bash
uv pip install --no-binary :all: numpy
```

**Result**: Failed due to missing build dependencies (autoreconf, etc.)

### 3. ❌ Using system Python (/usr/bin/python3)

```bash
uv venv --python /usr/bin/python3
```

**Result**: System Python was 3.10, but project requires 3.11+

### 4. ✅ Using pyenv-installed Python

```bash
# .envrc
PYENV_PYTHON="$HOME/.pyenv/versions/3.12.11/bin/python3"
uv venv --python "$PYENV_PYTHON"
```

**Result**: SUCCESS - pyenv Python is built against system libraries

## Proper Solutions (What We Should Have Done)

### Solution 1: nix-ld (System-wide)

Enable nix-ld in your NixOS configuration:

```nix
{
  programs.nix-ld = {
    enable = true;
    libraries = with pkgs; [
      stdenv.cc.cc.lib  # for libstdc++.so.6
      zlib              # for libz.so.1
      glibc             # for libm.so.6
    ];
  };
}
```

### Solution 2: fix-python Tool

```bash
# Install fix-python
nix profile install github:GuillaumeDesforges/fix-python

# Create venv with --copies flag
python -m venv .venv --copies
source .venv/bin/activate
pip install numpy pandas

# Fix the binaries
fix-python --venv .venv
```

### Solution 3: FHS User Environment

Create a `shell.nix`:

```nix
{ pkgs ? import <nixpkgs> {} }:
(pkgs.buildFHSUserEnv {
  name = "python-env";
  targetPkgs = pkgs: with pkgs; [
    python312
    gcc
    zlib
  ];
  runScript = "bash";
}).env
```

### Solution 4: Use Nix Python Packages

```bash
nix-shell -p "python312.withPackages(ps: with ps; [ numpy pandas ])"
```

### Solution 5: Poetry2nix or dream2nix

These tools automatically handle binary patching when building Python projects with Nix.

## Lessons Learned

1. **This is a known issue** - The Nix community has multiple solutions for this problem
2. **PyPI wheels assume FHS** - Binary wheels from PyPI expect libraries in standard Linux locations
3. **Nix Python + pip needs special handling** - Either use nix-ld, fix-python, or FHS environments
4. **pyenv as escape hatch** - pyenv-installed Python works because it's built against system libraries
5. **uv is not the issue** - The problem exists with pip, poetry, or any tool installing PyPI wheels

## Working .envrc (Current Solution)

```bash
# Use pyenv-installed Python 3.12.11 which is built against system libraries
# This will work with numpy wheels from PyPI
PYENV_PYTHON="$HOME/.pyenv/versions/3.12.11/bin/python3"
if [[ -x "$PYENV_PYTHON" ]]; then
    echo "[direnv] Using pyenv Python 3.12.11 for compatibility"
    PYTHON_FOR_VENV="$PYENV_PYTHON"
else
    PYTHON_FOR_VENV="python3"
fi

# Create venv with pyenv Python
if [[ ! -d .venv ]]; then
    echo "[direnv] Creating .venv with $PYTHON_FOR_VENV..."
    uv venv --python "$PYTHON_FOR_VENV"
fi

# Activate the venv
source .venv/bin/activate

# Install numpy/pandas if needed
if ! python -c "import numpy" 2>/dev/null; then
    echo "[direnv] Installing numpy/pandas..."
    uv pip install numpy pandas
fi

# Always install test/dev tooling for adgn_llm tasks
echo "[direnv] Installing adgn-llm into venv..."
uv pip install "llm/adgn_llm[test]"
```

## References

- [NixOS Wiki - Python](https://nixos.wiki/wiki/Python)
- [fix-python GitHub](https://github.com/GuillaumeDesforges/fix-python)
- [nix-ld GitHub](https://github.com/nix-community/nix-ld)
- [How to make Python dependencies work on NixOS](https://gist.github.com/GuillaumeDesforges/7d66cf0f63038724acf06f17331c9280)
