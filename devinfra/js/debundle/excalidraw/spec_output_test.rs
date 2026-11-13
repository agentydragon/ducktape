//! The spec's claimed modules and vendor swaps, asserted on `:debundle`'s
//! accepted output through the e2e helpers.

use std::fs;
use std::path::PathBuf;

use debundle_e2e_support::{
    assert_export_named_specifiers, assert_module_source, assert_node_output,
};
use runfiles::{Runfiles, rlocation};
use serde_json::Value;

fn out_root() -> PathBuf {
    let runfiles = Runfiles::create().expect("create runfiles");
    let out = std::env::var("DEBUNDLE_OUT").expect("DEBUNDLE_OUT");
    rlocation!(runfiles, out.as_str()).expect("resolve DEBUNDLE_OUT")
}

#[test]
fn claimed_modules_export_their_readable_names() {
    let app = out_root().join("app");
    for (module, names) in [
        (
            "editor/packages/excalidraw/appState.js",
            &["getDefaultAppState"][..],
        ),
        (
            "editor/packages/excalidraw/data/blob.js",
            &["blobToArrayBuffer"],
        ),
        (
            "editor/packages/excalidraw/element/textWrapping.js",
            &[
                "cachedCjkRegex",
                "cachedLineBreakRegex",
                "cachedEmojiRegex",
                "containsCJK",
                "getLineBreakRegex",
                "getEmojiRegex",
                "COMMON",
                "CJK",
                "EMOJI",
                "getLineBreakRegexSimple",
                "getLineBreakRegexAdvanced",
                "getEmojiRegexUnicode",
                "Regex",
                "Break",
                "parseTokens",
                "wrapText",
                "wrapLine",
                "wrapWord",
                "trimLine",
                "isSingleCharacter",
                "satisfiesWordInvariant",
            ],
        ),
        ("main/packages/excalidraw/components/Card.js", &["Card"]),
        (
            "main/packages/excalidraw/components/ColorPicker/HotkeyLabel.js",
            &["HotkeyLabel"],
        ),
        (
            "main/packages/excalidraw/components/ColorPicker/PickerHeading.js",
            &["PickerHeading"],
        ),
    ] {
        let source = fs::read_to_string(app.join(module)).expect(module);
        for name in names {
            assert_export_named_specifiers(&source, name, &[None]);
        }
    }
}

/// The only claimed module with no imports: the rest import their chunk
/// entry, which reads browser globals while it evaluates.
#[test]
fn blob_to_array_buffer_reads_a_blob_under_node() {
    let probe_dir = tempfile::tempdir().expect("tempdir");
    let probe = probe_dir.path().join("probe.mjs");
    let module = out_root().join("app/editor/packages/excalidraw/data/blob.js");
    fs::write(
        &probe,
        format!(
            "import {{ blobToArrayBuffer }} from {};\n\
             const buffer = await blobToArrayBuffer(new Blob([\"abc\"]));\n\
             console.log(new TextDecoder().decode(buffer));\n",
            serde_json::to_string(&format!("file://{}", module.display())).unwrap(),
        ),
    )
    .expect("write probe");
    assert_node_output(&probe, "abc\n", "");
}

#[test]
fn clsx_consumers_import_the_package() {
    let app = out_root().join("app");
    for entry in ["main/entry.js", "editor/entry.js"] {
        assert_module_source(
            &app,
            entry,
            &["from \"clsx\""],
            &["vendor/clsx_lib/entry.js"],
        );
    }
    // The partial swap strips the implementation once no consumer imports it.
    assert_module_source(&app, "vendor/clsx_lib/entry.js", &[], &["export"]);
}

#[test]
fn vendor_swap_report_lists_every_mark() {
    let report: Value = serde_json::from_slice(
        &fs::read(out_root().join("reports/vendor_swaps.json")).expect("read vendor_swaps.json"),
    )
    .expect("parse vendor_swaps.json");
    let keys = |section: &str| -> Vec<String> {
        report[section]
            .as_object()
            .expect(section)
            .keys()
            .cloned()
            .collect()
    };
    assert_eq!(keys("full"), ["vendor/clsx.js", "vendor/roughjs.js"]);
    assert_eq!(keys("partial"), ["vendor/clsx_lib.js"]);
    assert_eq!(
        report["partial"]["vendor/clsx_lib.js"]["symbols"]["a"]["references_rewritten"],
        2
    );
}
