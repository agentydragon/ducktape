use debundle_e2e_graph_support::json_node_declaring;
use debundle_e2e_support::run_debundle;
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Output;

pub(super) const CHAIN_SOURCE: &str = "const anchor = 1;\nconst dep = 2;\nconst consumer = dep + 1;\nconsole.log(anchor, consumer);\n";

pub(super) fn run(args: &[String]) -> Output {
    run_debundle(&args.iter().map(String::as_str).collect::<Vec<_>>())
}

pub(super) fn restore_tree(root: &Path, files: &std::collections::BTreeMap<PathBuf, Vec<u8>>) {
    if root.exists() {
        fs::remove_dir_all(root).unwrap();
    }
    for (relative, bytes) in files {
        let path = root.join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, bytes).unwrap();
    }
}

pub(super) fn unit_id_for_binding(graph: &Value, binding: &str) -> String {
    json_node_declaring(graph, binding)["id"]
        .as_str()
        .unwrap()
        .to_owned()
}

pub(super) fn graph_args(root: &Path, input_flag: &str, input: &Path) -> Vec<String> {
    vec![
        input_flag.into(),
        input.display().to_string(),
        "--modules".into(),
        root.join("modules").display().to_string(),
        "--source-root".into(),
        root.join("snapshot").display().to_string(),
        "--tree-config".into(),
        root.join("spec_config.yaml").display().to_string(),
    ]
}

pub(super) fn proposal_args(root: &Path, input_flag: &str, input: &Path) -> Vec<String> {
    let mut args = vec!["modules".into(), "propose".into()];
    args.extend(graph_args(root, input_flag, input));
    args.extend(["--format".into(), "json".into()]);
    args
}
