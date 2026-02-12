//! Reverse-engineered from process_api release process_api_2026-02-02-04-57
//! ELF Build ID: b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0
//!
//! /proc/{pid}/task/{tid}/children reader for building process trees.
//!
//! No definitive decompiled functions were found with unique string markers
//! for pid_tree.rs. The functionality was reconstructed from string evidence
//! at binary file offset 0x1aff9c ("/task/", "/children", "/build/src/pid_tree.rs")
//! and behavioral analysis of the orphan adoption and OOM killing code paths
//! that depend on PID tree traversal.

use std::collections::HashSet;
use std::path::Path;

/// Get all child PIDs of a given PID by reading /proc/{pid}/task/{tid}/children.
///
/// Reconstructed from string evidence at file offset 0x1aff9c:
///   "/task/", "/children", "/build/src/pid_tree.rs"
pub async fn get_child_pids(pid: u32) -> std::io::Result<Vec<u32>> {
    let task_dir = format!("/proc/{pid}/task");
    let mut children = Vec::new();

    let mut entries = tokio::fs::read_dir(&task_dir).await?;
    while let Some(entry) = entries.next_entry().await? {
        let tid = entry.file_name();
        let children_path = format!("/proc/{pid}/task/{}/children", tid.to_string_lossy());
        if let Ok(contents) = tokio::fs::read_to_string(&children_path).await {
            for token in contents.split_whitespace() {
                if let Ok(child_pid) = token.parse::<u32>() {
                    children.push(child_pid);
                }
            }
        }
    }

    Ok(children)
}

/// Recursively collect all descendant PIDs of a given PID.
pub async fn get_all_descendant_pids(pid: u32) -> std::io::Result<HashSet<u32>> {
    let mut all_descendants = HashSet::new();
    let mut stack = vec![pid];

    while let Some(current) = stack.pop() {
        if let Ok(children) = get_child_pids(current).await {
            for child in children {
                if all_descendants.insert(child) {
                    stack.push(child);
                }
            }
        }
    }

    Ok(all_descendants)
}

/// Check if a PID exists by checking /proc/{pid}/status.
pub fn pid_exists(pid: u32) -> bool {
    Path::new(&format!("/proc/{pid}/status")).exists()
}
