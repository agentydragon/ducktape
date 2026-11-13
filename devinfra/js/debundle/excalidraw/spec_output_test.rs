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
fn claimed_modules_export_their_entry_points() {
    let app = out_root().join("app");
    for (module, names) in [
        (
            "editor/packages/excalidraw/appState.js",
            &["getDefaultAppState"][..],
        ),
        (
            "editor/packages/excalidraw/data/blob.js",
            &[
                "blobToArrayBuffer",
                "getDataURL",
                "normalizeFile",
                "loadFromBlob",
                "parseLibraryJSON",
            ],
        ),
        (
            "editor/packages/excalidraw/element/textWrapping.js",
            &["containsCJK", "wrapText"],
        ),
        ("editor/packages/excalidraw/scene/Scene.js", &["Scene"]),
        ("main/packages/excalidraw/components/Card.js", &["Card"]),
        (
            "main/packages/excalidraw/history.js",
            &["History", "HistoryChangedEvent"],
        ),
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
    assert_module_source(
        &app,
        "editor/packages/excalidraw/element/textWrapping.js",
        &["var parseTokens =", "var Regex =", "var Break ="],
        &[],
    );
    assert_module_source(
        &app,
        "main/packages/excalidraw/history.js",
        &["var HistoryEntry ="],
        &[],
    );
    assert_module_source(
        &app,
        "editor/entry.js",
        &[
            "Scene, \"sceneMapByElement\", new WeakMap",
            "Scene, \"sceneMapById\", new Map",
        ],
        &[],
    );
}

#[test]
fn recovered_math_modules_run_without_the_browser_entry() {
    let probe_dir = tempfile::tempdir().expect("tempdir");
    let probe = probe_dir.path().join("probe.mjs");
    let math = out_root().join("app/editor/packages/math");
    let module_url = |name: &str| {
        serde_json::to_string(&format!("file://{}", math.join(name).display())).unwrap()
    };
    fs::write(
        &probe,
        format!(
            r#"
import * as point from {point};
import * as vector from {vector};
import * as angle from {angle};
import * as utils from {utils};
console.log(JSON.stringify([
  point.pointDistance([0, 0], [3, 4]),
  point.pointsEqual(point.pointRotateRads([1, 0], [0, 0], Math.PI / 2), [0, 1]),
  point.pointScaleFromOrigin([3, 5], [1, 1], 2),
  point.isPointWithinBounds([0, 0], [1, 1], [2, 2]),
  point.isPoint([1, NaN]),
  vector.vectorNormalize([3, 4]),
  vector.vectorNormalize([0, 0]),
  vector.vectorCross([1, 0], [0, 1]),
  vector.vectorDot([1, 2], [3, 4]),
  angle.degreesToRadians(180) === Math.PI,
  angle.isRightAngleRads(Math.PI / 2),
  utils.clamp(12, 0, 10),
  utils.round(1.234, 2),
  utils.roundToStep(1.25, 0.5),
  utils.isFiniteNumber(Infinity),
  utils.isCloseTo(1, 1.00001),
]));
"#,
            point = module_url("point.js"),
            vector = module_url("vector.js"),
            angle = module_url("angle.js"),
            utils = module_url("utils.js"),
        ),
    )
    .expect("write probe");
    assert_node_output(
        &probe,
        "[5,true,[5,9],true,false,[0.6,0.8],[0,0],1,11,true,true,10,1.23,1.5,false,true]\n",
        "",
    );
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
