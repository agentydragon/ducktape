# Native Lean modules

`native_lean_module` builds one Lean module's `.olean`, generated C, and native object in separate Bazel actions. Its
`module` is the full Lean name; `src` must end in the corresponding path. List imported project modules as `deps`.
The rule stages their transitive `.olean` files by full module name and rejects two different artifacts for the same
name. `native_lean_binary` links the root module's transitive objects.

The toolchain patch exposes the pinned Lean distribution's Clang as `native_cc`; the other declared toolchain files
supply its sysroot, headers and libraries. The flags in `native.bzl` are the Linux flags of the pinned Lean 4.35
distribution and need review when that distribution changes. This first rule is for native modules that compile with
those inputs. It does not support plugin libraries, custom link dependencies, or Lean runtime imports requiring
additional artifacts. The binary rule supplies no Lean compiler or `.olean` runfiles; programs needing them must
explicitly package them using an appropriate launcher.

The CoreJS test builds `Syntax` and `Fragment` as separate modules. It compares a native entry's results with the
existing interpreted entry and runs the native entry with an empty environment and a nonexistent `PATH`. This checks
the module boundary, not the splitter's certification or its generated-proof fallback. The production splitter
and its Lean verification runfiles remain unchanged.
