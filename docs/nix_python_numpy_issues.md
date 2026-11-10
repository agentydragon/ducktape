# Historical Nix Python / NumPy wheel failures

This records an earlier local failure, not a current Python setup recipe. The environment was
Nix Python 3.12.11 with a direnv-managed `uv` virtualenv; NumPy 2.3.2 (and pandas) failed to
load PyPI wheels with missing `libstdc++.so.6` or `libz.so.1`. NumPy also emitted its misleading
“you should not try to import numpy from its source directory” message; the original error was a
missing shared library, not the working directory. The detailed NumPy traceback named
`/home/agentydragon/code/ducktape/.venv/bin/python`.

The observed attempts were:

| Attempt                                                               | Result                                                                                                                                                                  |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Add Nix store paths to `LD_LIBRARY_PATH`                              | Partial import success; fragile because the exact store paths must be found.                                                                                            |
| Build NumPy from source with `uv pip install --no-binary :all: numpy` | Failed on missing build tools such as `autoreconf`.                                                                                                                     |
| Use `/usr/bin/python3`                                                | Host Python was 3.10, below this project’s then-required 3.11.                                                                                                          |
| Use `$HOME/.pyenv/versions/3.12.11/bin/python3` for the venv          | Import succeeded in this observed run because it linked against system libraries. This is a historical escape hatch, not the repository’s current environment contract. |

The failure was in PyPI wheels expecting standard Linux library locations while Nix keeps
libraries under `/nix/store`; it was not specific to `uv`. The old embedded `.envrc` and its
`adgn_llm` install are obsolete. The repository’s current [`.envrc`](../.envrc) loads the flake devShell and
preserves a host-selected `SSL_CERT_FILE`; it does not create that Python venv. The NixOS
downloaded-toolchain linker setup is owned by [`nix/nixos/modules/bazel/default.nix`](../nix/nixos/modules/bazel/default.nix),
which enables `nix-ld` for Bazel’s dynamically linked tools; that setting alone does not establish
that every arbitrary PyPI wheel works in a venv.

References: [NumPy import troubleshooting](https://numpy.org/doc/stable/user/troubleshooting-importerror.html),
[NixOS Python guidance](https://nixos.wiki/wiki/Python), and
[nix-ld](https://github.com/nix-community/nix-ld).
