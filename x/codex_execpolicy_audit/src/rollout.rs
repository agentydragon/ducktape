//! Walk a codex `CODEX_HOME/sessions` tree and extract every `exec_command`
//! command string from rollout JSONL files. Each rollout line is a record with
//! a `payload`; we keep `response_item`s whose payload is a `function_call` named
//! `exec_command` and parse `arguments.cmd`.
use anyhow::Result;
use serde_json::Value;
use std::io::BufRead;
use std::path::Path;

pub fn collect_cmds(codex_home: &Path) -> Result<Vec<String>> {
    let mut cmds = Vec::new();
    walk(&codex_home.join("sessions"), &mut cmds)?;
    Ok(cmds)
}

fn walk(dir: &Path, cmds: &mut Vec<String>) -> Result<()> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Ok(()); // no sessions dir yet — nothing to audit
    };
    for entry in entries {
        let entry = entry?;
        let p = entry.path();
        if p.is_dir() {
            walk(&p, cmds)?;
        } else if p
            .file_name()
            .is_some_and(|f| f.to_string_lossy().starts_with("rollout-"))
        {
            let _ = collect_file(&p, cmds); // one bad rollout file shouldn't abort the audit
        }
    }
    Ok(())
}

fn collect_file(path: &Path, cmds: &mut Vec<String>) -> Result<()> {
    let reader = std::io::BufReader::new(std::fs::File::open(path)?);
    for line in reader.lines() {
        let line = line?;
        if !line.contains("\"exec_command\"") {
            continue; // fast pre-filter before parsing JSON
        }
        let Ok(v) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        let Some(payload) = v.get("payload") else {
            continue;
        };
        if payload.get("type").and_then(|t| t.as_str()) != Some("function_call") {
            continue;
        }
        if payload.get("name").and_then(|n| n.as_str()) != Some("exec_command") {
            continue;
        }
        let Some(args_str) = payload.get("arguments").and_then(|a| a.as_str()) else {
            continue;
        };
        let Ok(args) = serde_json::from_str::<Value>(args_str) else {
            continue;
        };
        if let Some(cmd) = args.get("cmd").and_then(|c| c.as_str()) {
            if !cmd.trim().is_empty() {
                cmds.push(cmd.to_string());
            }
        }
    }
    Ok(())
}
