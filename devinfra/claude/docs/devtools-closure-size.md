# `.#devtools` Nix Closure Size Breakdown

Generated 2026-03-24. Total closure: **543.5 MiB**.

Regenerate with: `devinfra/claude/docs/devtools-closure-size.sh`

## Top paths by NAR size

|      Size | Cumul% | Store path                        |
| --------: | -----: | --------------------------------- |
| 106.7 MiB |  19.6% | `python3-3.13.12`                 |
|  52.7 MiB |  29.3% | `gh-2.83.2`                       |
|  50.2 MiB |  38.6% | `git-minimal-2.51.2`              |
|  45.7 MiB |  47.0% | `python3.13-kubernetes-33.1.0`    |
|  42.3 MiB |  54.7% | `grpc-1.76.0`                     |
|  38.2 MiB |  61.8% | `icu4c-76.1`                      |
|  28.8 MiB |  67.1% | `glibc-2.40-218`                  |
|  18.4 MiB |  70.4% | `gettext-0.25.1`                  |
|  17.9 MiB |  73.7% | `protobuf-32.1`                   |
|  17.3 MiB |  76.9% | `bbapi-latest`                    |
|  11.8 MiB |  79.1% | `python3.13-pygments-2.19.2`      |
|   9.5 MiB |  80.8% | `gcc-14.3.0-lib`                  |
|   8.9 MiB |  82.5% | `openssl-3.6.1`                   |
|   7.2 MiB |  83.8% | `abseil-cpp-20250814.1`           |
|   7.1 MiB |  85.1% | `bash-interactive-5.3p3`          |
|   7.0 MiB |  86.4% | `python3.13-virtualenv-20.33.1`   |
|   5.7 MiB |  87.5% | `icu4c-76.1-dev`                  |
|   5.5 MiB |  88.5% | `python3.13-cryptography-46.0.5`  |
|   5.4 MiB |  89.5% | `python3.13-supervisor-4.3.0`     |
|   5.3 MiB |  90.4% | `python3.13-pydantic-core-2.33.2` |
|   5.3 MiB |  91.4% | `python3.13-pydantic-2.11.7`      |
|   4.2 MiB |  92.2% | `python3.13-pytest-8.4.2`         |
|   3.8 MiB |  92.9% | `sqlite-3.50.4`                   |
|   3.6 MiB |  93.5% | `ncurses-6.5`                     |
|   3.3 MiB |  94.1% | `python3.13-protobuf-6.33.1`      |
|   3.2 MiB |  94.7% | `python3.13-rich-14.1.0`          |
|   2.9 MiB |  95.3% | `python3.13-psutil-7.1.2`         |
|   2.9 MiB |  95.8% | `python3.13-pyasn1-modules-0.4.2` |
|   2.7 MiB |  96.3% | `krb5-1.22.1-lib`                 |
|   2.3 MiB |  96.7% | `python3.13-google-auth-2.41.1`   |
|   2.0 MiB |  97.1% | `tzdata-2025c`                    |
|   2.0 MiB |  97.4% | `libunistring-1.4.1`              |
|   2.0 MiB |  97.8% | `pcre2-10.46`                     |
|   1.9 MiB |  98.2% | `util-linux-minimal-2.41.3-lib`   |
|   1.8 MiB |  98.5% | `bash-5.3p3`                      |
|   1.8 MiB |  98.8% | `python3.13-pygit2-1.18.2`        |
|   1.7 MiB |  99.1% | `python3.13-pycparser-2.23`       |
|   1.6 MiB |  99.4% | `python3.13-anyio-4.11.0`         |
|   1.6 MiB |  99.7% | `libgit2-1.9.2-lib`               |
|   1.6 MiB | 100.0% | `python3.13-oauthlib-3.3.1`       |

## Dependency graph (what pulls what)

```
devtools (symlinkJoin)
├── claude-hooks (buildPythonApplication from wheel)
│   ├── kubernetes → googleapis-common-protos → grpc (42 MiB)
│   │                                           ├── protobuf C++ (18 MiB)
│   │                                           │   └── abseil-cpp (7 MiB, 4.4 MiB headers)
│   │                                           ├── icu4c (38 MiB) + icu4c-dev (5.7 MiB headers)
│   │                                           └── re2 + re2-dev
│   ├── pre-commit (propagatedBuildInput — pulls pytest, virtualenv, pygments)
│   │   ├── pytest (4.2 MiB)
│   │   ├── virtualenv (7.0 MiB)
│   │   └── pygments (11.8 MiB, also via rich + identify)
│   └── [other python deps: pydantic, rich, cryptography, supervisor, ...]
├── bbapi (17.3 MiB Go binary, already stripped)
├── gh (52.7 MiB Go binary, already stripped)
│   └── git-minimal (50.2 MiB)
│       ├── gettext (18.4 MiB — full package, only libintl needed at runtime)
│       ├── bash-interactive (7.1 MiB — 5.6 MiB locale; plain bash already in closure)
│       └── share/locale (13 MiB)
└── skills (tarball)
```

## Stripping verdict

All `.so` files and ELF binaries are **already stripped** by nixpkgs (`strip --strip-unneeded`).
`file` reports "not stripped" because `.symtab` is present (required for dynamic linking),
but there are zero `.debug_*` sections. Measured savings from `strip`: **0 bytes** across
all top libs (libicudata, libgrpc, libprotoc, libpython, libcrypto, libstdc++, gh, bbapi).

## Waste breakdown

### Dev outputs leaking into runtime closure (~15 MiB)

Headers and dev packages that are build-time artifacts, not needed at runtime:

|    Size | Path                             | Pulled by                     |
| ------: | -------------------------------- | ----------------------------- |
| 5.7 MiB | `icu4c-76.1-dev` (headers + man) | `re2-dev` → `grpc`            |
| 5.3 MiB | `protobuf-32.1/include/`         | `grpc`                        |
| 4.4 MiB | `abseil-cpp/include/`            | `protobuf` → `grpc`           |
| 2.0 MiB | `grpc/include/`                  | `claude-hooks` → `kubernetes` |
| 2.4 MiB | `python3/include/`               | stdlib                        |

**Fix**: Override `grpc` to depend on `re2` runtime output instead of `re2-dev`. The
dev→runtime leak starts at re2-dev referencing icu4c-dev.

### Locale/i18n data (~40 MiB)

|    Size | Path                                        |
| ------: | ------------------------------------------- |
|  13 MiB | `git-minimal/share/locale`                  |
|  13 MiB | `glibc/share/i18n/locales`                  |
| 5.6 MiB | `bash-interactive/share/locale`             |
| 4.5 MiB | `gettext/share/locale`                      |
|  ~4 MiB | various (grep, sed, xz, gdbm, libidn2, ...) |

**Fix**: Override `git-minimal` with `installFlags = ["NO_GETTEXT=1"]` or use a
locale-stripped git. Override glibc with `allLocales = false`.

### `pre-commit` transitive deps (~23 MiB)

`pre-commit` is a `propagatedBuildInput` of `claude-hooks`, so its entire Python dep
tree (pytest, virtualenv, pygments) leaks into the closure. Pre-commit is a CLI tool,
not a library import.

**Fix**: Move `pre-commit` out of `propagatedBuildInputs` in `claude-hooks.nix`. Either:

- Add `pkgs.pre-commit` directly to the `devtools` `symlinkJoin` paths
- Use `makeWrapperArgs` to inject it into `claude-hook`'s `PATH`

### `gettext` full package (18.4 MiB, only ~1 MiB needed)

Git only needs `libintl.so` at runtime. The closure includes the full gettext with
`xgettext` (8.3 MiB), `msgfmt`, locale data (4.5 MiB), and build libraries.

**Fix**: Override `git-minimal` to depend on `gettext.lib` output (just `libintl`)
instead of the full `gettext` package.

### `bash-interactive` (7.1 MiB, redundant)

Plain `bash` (1.8 MiB) is already in the closure. `bash-interactive` adds readline
support and 5.6 MiB of locale data. Pulled by `git-minimal`.

**Fix**: Override `git-minimal` to reference `bash` instead of `bash-interactive`.

### Test suites shipped in Python packages (~7 MiB)

|    Size | Path                           |
| ------: | ------------------------------ |
| 2.5 MiB | `supervisor/tests/`            |
| 2.4 MiB | `python3/lib/python3.13/test/` |
| 1.9 MiB | `psutil/tests/`                |

Small individually, but adds up. Hard to fix without per-package overlays.

## Reduction opportunities (ranked)

| Est. savings | Effort | Action                                               |
| -----------: | ------ | ---------------------------------------------------- |
|      ~23 MiB | Low    | Move `pre-commit` out of `propagatedBuildInputs`     |
|      ~40 MiB | Medium | Strip locale data from git, glibc, bash, gettext     |
|      ~18 MiB | Medium | Override git to use `gettext.lib` not full `gettext` |
|      ~15 MiB | Medium | Fix dev output leak (re2-dev → re2 in grpc overlay)  |
|       ~5 MiB | Low    | Override git to use `bash` not `bash-interactive`    |
|       ~0 MiB | N/A    | ~~Strip binaries~~ — already stripped, no savings    |

**Total estimated reduction: ~100 MiB (18%)** from 543 → ~440 MiB.
