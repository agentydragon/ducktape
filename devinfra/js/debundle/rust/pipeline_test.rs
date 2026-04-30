use std::fs;

use anyhow::Result;

mod ast_ir;
mod emit;
mod owner_graph;
mod pipeline;
mod plan;

#[test]
fn writes_manifest_for_mock_bundle_layout() -> Result<()> {
    let input = tempfile::tempdir()?;
    let out = tempfile::tempdir()?;

    fs::create_dir_all(input.path().join("static"))?;
    fs::write(
        input.path().join("static/index-DuckMock.js"),
        "export const x = 1;\n",
    )?;
    fs::write(
        input.path().join("static/chunk-DuckMock.js"),
        "export const y = 2;\n",
    )?;
    fs::write(
        input.path().join("js-files.txt"),
        "static/index-DuckMock.js\nstatic/chunk-DuckMock.js\n",
    )?;

    pipeline::run(&pipeline::Cli {
        input_root: input.path().to_path_buf(),
        js_list: input.path().join("js-files.txt"),
        out_root: out.path().to_path_buf(),
    })?;

    let manifest_text = fs::read_to_string(out.path().join("manifest.json"))?;
    let parsed: serde_json::Value = serde_json::from_str(&manifest_text)?;
    assert_eq!(parsed["schemaVersion"], 1);
    assert_eq!(parsed["scriptSource"], "split");
    assert!(out.path().join("bootstrap.js").exists());
    assert!(out.path().join("chunks.manifest.json").exists());
    assert!(out.path().join("static/index-DuckMock/entry.js").exists());
    assert!(out.path().join("static/chunk-DuckMock/entry.js").exists());
    Ok(())
}
