# Experimental runtime slicing for the exact pinned formatter. New upstream
# layouts or diagnostic references deliberately fail these guards for review.
{ pkgs }:
let
  rustcPath = toString pkgs.rustc.unwrapped;
  runtimeLibs = pkgs.lib.makeLibraryPath [
    pkgs.glibc
    pkgs.stdenv.cc.cc.lib
    pkgs.libxml2
    pkgs.libffi
    pkgs.zlib
  ];
in
pkgs.runCommand "rustfmt-runtime-${pkgs.rustfmt.version}"
  {
    nativeBuildInputs = [
      pkgs.patchelf
      pkgs.binutils
      pkgs.removeReferencesTo
    ];
    disallowedRequisites = [
      pkgs.rustc.unwrapped
      pkgs.llvmPackages.llvm.lib
      pkgs.rustfmt
    ];
  }
  ''
    set -euo pipefail
    mkdir -p "$out/bin" "$out/lib"
    cp -- ${pkgs.rustfmt}/bin/rustfmt "$out/bin/rustfmt"
    cp -- ${pkgs.rustc.unwrapped}/lib/rustlib/x86_64-unknown-linux-gnu/lib/librustc_driver-12669708ccd0802b.so "$out/lib/"
    cp -- ${pkgs.llvmPackages.llvm.lib}/lib/libLLVM.so.21.1 "$out/lib/"
    chmod u+w "$out/bin/rustfmt" "$out/lib/librustc_driver-12669708ccd0802b.so" "$out/lib/libLLVM.so.21.1"
    patchelf --set-rpath '$ORIGIN/../lib:${runtimeLibs}' "$out/bin/rustfmt"
    patchelf --set-rpath '$ORIGIN:${runtimeLibs}' "$out/lib/librustc_driver-12669708ccd0802b.so"
    patchelf --set-rpath '$ORIGIN:${runtimeLibs}' "$out/lib/libLLVM.so.21.1"

    rustc_path='${rustcPath}'
    matches="$(strings -a "$out/bin/rustfmt" | grep -F "$rustc_path" || true)"
    mapfile -t refs <<< "$matches"
    test "''${#refs[@]}" -eq 6 || {
      printf 'expected exactly six rustc-source references; got %s:\n%s\n' "''${#refs[@]}" "$matches" >&2
      exit 1
    }
    for ref in "''${refs[@]}"; do
      case "$ref" in
        "$rustc_path"/lib/rustlib/rustc-src/rust/compiler/rustc_errors/src/emitter.rs|\
        "$rustc_path"/lib/rustlib/rustc-src/rust/compiler/rustc_parse/src/lib.rs|\
        "$rustc_path"/lib/rustlib/rustc-src/rust/compiler/rustc_span/src/source_map.rs|\
        "$rustc_path"/lib/rustlib/rustc-src/rust/compiler/rustc_errors/src/diagnostic.rs|\
        "$rustc_path"/lib/rustlib/rustc-src/rust/compiler/rustc_span/src/hygiene.rs|\
        "$rustc_path"/lib/rustlib/rustc-src/rust/compiler/rustc_span/src/span_encoding.rs) ;;
        *) printf 'unexpected rustc path reference: %s\n' "$ref" >&2; exit 1 ;;
      esac
    done
    remove-references-to -t ${pkgs.rustc.unwrapped} "$out/bin/rustfmt"
    chmod 0555 "$out/bin/rustfmt" "$out/lib/librustc_driver-12669708ccd0802b.so" "$out/lib/libLLVM.so.21.1"
  ''
