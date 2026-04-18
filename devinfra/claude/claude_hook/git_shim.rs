//! Git shim block logic — ports `server.py::_handle_git_shim`.
//!
//! Given the original argv and the git shim config, decide whether to
//! pass through (`Ok(argv)`) or block (`Err(message)`). Callers propagate
//! `Err` into a `ShimResponse::Blocked`.

use claude_hook_config::GitShimConfig;

const GIT_GLOBAL_VALUE_OPTIONS: &[&str] = &[
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--super-prefix",
];

/// Parse git global options to find the subcommand and its args.
/// Returns (subcommand, sub_args).
fn extract_subcommand(args: &[String]) -> (Option<&str>, &[String]) {
    let mut i = 0;
    while i < args.len() {
        let arg = &args[i];
        if arg.starts_with("--") && arg.contains('=') {
            let key = arg.split_once('=').unwrap().0;
            if GIT_GLOBAL_VALUE_OPTIONS.contains(&key) {
                i += 1;
                continue;
            }
        }
        if GIT_GLOBAL_VALUE_OPTIONS.contains(&arg.as_str()) {
            i += 2;
            continue;
        }
        if arg.starts_with('-') {
            i += 1;
            continue;
        }
        return (Some(arg.as_str()), &args[i + 1..]);
    }
    (None, &[])
}

/// Evaluate the git shim policy. `argv` is the full invocation (argv[0] =
/// "git"); sub-args are everything after the subcommand name.
pub fn evaluate(argv: &[String], cfg: &GitShimConfig) -> Result<(), String> {
    if argv.len() < 2 {
        return Ok(());
    }
    let (subcommand, sub_args) = extract_subcommand(&argv[1..]);
    let Some(sub) = subcommand else { return Ok(()) };

    if cfg.block_add_all && sub == "add" {
        let blocked = if sub_args.iter().any(|a| a == "--all") {
            Some("git add --all")
        } else if sub_args.iter().any(|a| a == "-A") {
            Some("git add -A")
        } else if let Some(arg) = sub_args
            .iter()
            .find(|a| a.starts_with('-') && !a.starts_with("--") && a.contains('A'))
        {
            return Err(format!(
                "git add {arg} (contains -A)\n  Use 'git add <specific-files>' instead of staging everything."
            ));
        } else if sub_args.iter().any(|a| a == ".") {
            Some("git add .")
        } else {
            None
        };
        if let Some(cmd) = blocked {
            return Err(format!(
                "{cmd}\n  Use 'git add <specific-files>' instead of staging everything."
            ));
        }
    }

    if cfg.block_stash && sub == "stash" {
        let first_positional = sub_args.iter().find(|a| !a.starts_with('-'));
        match first_positional.map(|s| s.as_str()) {
            Some("list") | Some("show") => {}
            _ => {
                return Err(
                    "git stash\n  Do not use git stash. Find other approaches for dirty worktrees."
                        .into(),
                );
            }
        }
    }

    if cfg.block_amend && sub == "commit" && sub_args.iter().any(|a| a == "--amend") {
        return Err("git commit --amend\n  Create a new commit instead of amending.".into());
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|s| s.to_string()).collect()
    }

    fn block_add_all() -> GitShimConfig {
        GitShimConfig {
            block_add_all: true,
            ..Default::default()
        }
    }

    #[test]
    fn add_file_passes() {
        assert!(evaluate(&argv(&["git", "add", "foo.txt"]), &block_add_all()).is_ok());
    }

    #[test]
    fn add_dash_a_blocks() {
        let e = evaluate(&argv(&["git", "add", "-A"]), &block_add_all()).unwrap_err();
        assert!(e.contains("git add -A"));
    }

    #[test]
    fn add_dashall_blocks() {
        let e = evaluate(&argv(&["git", "add", "--all"]), &block_add_all()).unwrap_err();
        assert!(e.contains("git add --all"));
    }

    #[test]
    fn add_dot_blocks() {
        let e = evaluate(&argv(&["git", "add", "."]), &block_add_all()).unwrap_err();
        assert!(e.contains("git add ."));
    }

    #[test]
    fn add_combined_short_flag_with_a_blocks() {
        let e = evaluate(&argv(&["git", "add", "-Av"]), &block_add_all()).unwrap_err();
        assert!(e.contains("-Av"));
    }

    #[test]
    fn global_options_skipped() {
        // -C <dir> consumes the next arg as a value; parser should still find "add".
        let e = evaluate(&argv(&["git", "-C", "/tmp", "add", "-A"]), &block_add_all()).unwrap_err();
        assert!(e.contains("git add -A"));
    }

    #[test]
    fn stash_list_allowed() {
        let cfg = GitShimConfig {
            block_stash: true,
            ..Default::default()
        };
        assert!(evaluate(&argv(&["git", "stash", "list"]), &cfg).is_ok());
        assert!(evaluate(&argv(&["git", "stash", "show"]), &cfg).is_ok());
    }

    #[test]
    fn stash_push_blocked() {
        let cfg = GitShimConfig {
            block_stash: true,
            ..Default::default()
        };
        assert!(evaluate(&argv(&["git", "stash"]), &cfg).is_err());
        assert!(evaluate(&argv(&["git", "stash", "push"]), &cfg).is_err());
    }

    #[test]
    fn commit_amend_blocked() {
        let cfg = GitShimConfig {
            block_amend: true,
            ..Default::default()
        };
        assert!(evaluate(&argv(&["git", "commit", "--amend"]), &cfg).is_err());
        assert!(evaluate(&argv(&["git", "commit", "-m", "x"]), &cfg).is_ok());
    }

    #[test]
    fn disabled_config_allows_everything() {
        let cfg = GitShimConfig::default();
        assert!(evaluate(&argv(&["git", "add", "-A"]), &cfg).is_ok());
        assert!(evaluate(&argv(&["git", "stash"]), &cfg).is_ok());
        assert!(evaluate(&argv(&["git", "commit", "--amend"]), &cfg).is_ok());
    }
}
