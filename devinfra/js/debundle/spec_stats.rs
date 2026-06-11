//! Spec-wide aggregate stats: module + binding totals in one walk.
//!
//! Powers `debundle spec stats`. Operates over the same on-disk modules
//! tree that `debundle modules list` and `debundle bindings list`
//! consume, so the per-row counters here match those commands by
//! construction (orphan = singleton-module member, unrenamed = no
//! readable `name:`, etc.).
//!
//! The bucket boundaries (`singletons` / `tiny_2_to_5` /
//! `medium_6_to_20` / `large_21_plus`) are the ones that survived the
//! 2026-05-26 corpus survey of a real spec — see the `CLI gaps`
//! section of `TODO.md`. They are intentionally exposed as a fixed
//! shape: spec-wide stats lose value once each consumer picks its own
//! bucketing.

use std::path::Path;

use anyhow::{Context, Result};
use serde::Serialize;

use spec_modules::{
    ModuleFile, collect_module_files, is_residual_module_path, module_path_from_file,
    read_module_file,
};

/// Member-count buckets per the 2026-05-26 real-spec survey.
#[derive(Debug, Clone, Default, Serialize)]
pub struct MemberCountBuckets {
    /// Smallest member count observed across all modules. `0` when
    /// the spec contains an empty module.
    pub min: usize,
    /// Largest member count observed across all modules.
    pub max: usize,
    /// Modules with exactly one member.
    pub singletons: usize,
    /// Modules with 2..=5 members.
    pub tiny_2_to_5: usize,
    /// Modules with 6..=20 members.
    pub medium_6_to_20: usize,
    /// Modules with 21+ members.
    pub large_21_plus: usize,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct ModulesStats {
    pub total: usize,
    pub residual: usize,
    pub empty: usize,
    pub with_comment: usize,
    pub member_count: MemberCountBuckets,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct BindingsStats {
    pub total: usize,
    pub renamed: usize,
    pub unrenamed: usize,
    pub orphan: usize,
    pub with_comment: usize,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct SpecStats {
    pub modules: ModulesStats,
    pub bindings: BindingsStats,
}

/// Bucket a single module's member count. Mutates the passed
/// [`MemberCountBuckets`] in place (the caller maintains the running
/// aggregate across the spec).
pub fn bucket_member_count(buckets: &mut MemberCountBuckets, count: usize, first: bool) {
    if first {
        buckets.min = count;
        buckets.max = count;
    } else {
        if count < buckets.min {
            buckets.min = count;
        }
        if count > buckets.max {
            buckets.max = count;
        }
    }
    match count {
        1 => buckets.singletons += 1,
        2..=5 => buckets.tiny_2_to_5 += 1,
        6..=20 => buckets.medium_6_to_20 += 1,
        n if n >= 21 => buckets.large_21_plus += 1,
        _ => {}
    }
}

/// Compute spec-wide stats with one pass over `modules_root`.
///
/// Determinism: every counter only reads `ModuleFile` shape — no
/// HashMap iteration, no `read_dir` ordering leaks into the totals.
/// `collect_module_files` already returns sorted paths, but the
/// counters are commutative + associative anyway, so the per-walk
/// order doesn't matter.
pub fn compute_spec_stats(modules_root: &Path) -> Result<SpecStats> {
    let files = collect_module_files(modules_root)
        .with_context(|| format!("walking {}", modules_root.display()))?;

    let mut modules = ModulesStats::default();
    let mut bindings = BindingsStats::default();

    let mut first_module = true;
    for file in &files {
        let module: ModuleFile = read_module_file(file)?;
        let path = module_path_from_file(file, modules_root);
        let member_count = module.members.len();
        let is_residual = is_residual_module_path(&path);

        modules.total += 1;
        if is_residual {
            modules.residual += 1;
        }
        // "Empty" matches the `modules delete` predicate: no
        // members AND no anonymous_statements. A module with
        // anonymous_statements only is not deletable without
        // `--force` and not "empty" in any meaningful sense.
        if member_count == 0 && module.anonymous_statements.is_empty() {
            modules.empty += 1;
        }
        if module.comment.is_some() {
            modules.with_comment += 1;
        }
        bucket_member_count(&mut modules.member_count, member_count, first_module);
        first_module = false;

        for member in &module.members {
            bindings.total += 1;
            if member.name.is_some() {
                bindings.renamed += 1;
            } else {
                bindings.unrenamed += 1;
            }
            if member_count <= 1 {
                bindings.orphan += 1;
            }
            if member.comment.is_some() {
                bindings.with_comment += 1;
            }
        }
    }

    // If the modules root contains zero files, the `first_module` flag
    // never flipped — leave the buckets at their `Default` (all-zero)
    // shape. That matches the "empty spec" intuition for min/max.

    Ok(SpecStats { modules, bindings })
}

/// Human-readable text rendering: a compact two-section summary.
pub fn render_spec_stats_text(stats: &SpecStats, out: &mut String) {
    let m = &stats.modules;
    let b = &stats.bindings;
    out.push_str("modules:\n");
    out.push_str(&format!("  total          {}\n", m.total));
    out.push_str(&format!("  residual       {}\n", m.residual));
    out.push_str(&format!("  empty          {}\n", m.empty));
    out.push_str(&format!("  with_comment   {}\n", m.with_comment));
    out.push_str("  member_count:\n");
    out.push_str(&format!("    min            {}\n", m.member_count.min));
    out.push_str(&format!("    max            {}\n", m.member_count.max));
    out.push_str(&format!(
        "    singletons     {}\n",
        m.member_count.singletons
    ));
    out.push_str(&format!(
        "    tiny_2_to_5    {}\n",
        m.member_count.tiny_2_to_5
    ));
    out.push_str(&format!(
        "    medium_6_to_20 {}\n",
        m.member_count.medium_6_to_20
    ));
    out.push_str(&format!(
        "    large_21_plus  {}\n",
        m.member_count.large_21_plus
    ));
    out.push_str("bindings:\n");
    out.push_str(&format!("  total          {}\n", b.total));
    out.push_str(&format!("  renamed        {}\n", b.renamed));
    out.push_str(&format!("  unrenamed      {}\n", b.unrenamed));
    out.push_str(&format!("  orphan         {}\n", b.orphan));
    out.push_str(&format!("  with_comment   {}\n", b.with_comment));
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    fn write(root: &Path, rel: &str, body: &str) {
        let p = root.join(rel);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, body).unwrap();
    }

    #[test]
    fn single_module_one_binding() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "a.yaml",
            "members:\n  - selector: { binding: { name: a } }\n",
        );
        let s = compute_spec_stats(root).unwrap();
        assert_eq!(s.modules.total, 1);
        assert_eq!(s.modules.residual, 0);
        assert_eq!(s.modules.empty, 0);
        assert_eq!(s.modules.with_comment, 0);
        assert_eq!(s.modules.member_count.min, 1);
        assert_eq!(s.modules.member_count.max, 1);
        assert_eq!(s.modules.member_count.singletons, 1);
        assert_eq!(s.modules.member_count.tiny_2_to_5, 0);
        assert_eq!(s.bindings.total, 1);
        assert_eq!(s.bindings.unrenamed, 1);
        assert_eq!(s.bindings.renamed, 0);
        assert_eq!(s.bindings.orphan, 1);
    }

    #[test]
    fn singleton_plus_multi_member_module() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "solo.yaml",
            "members:\n  - name: Solo\n    selector: { binding: { name: a } }\n",
        );
        write(
            root,
            "pair.yaml",
            "members:\n  - selector: { binding: { name: b } }\n  - selector: { binding: { name: c } }\n",
        );
        let s = compute_spec_stats(root).unwrap();
        assert_eq!(s.modules.total, 2);
        assert_eq!(s.modules.member_count.singletons, 1);
        assert_eq!(s.modules.member_count.tiny_2_to_5, 1);
        assert_eq!(s.modules.member_count.min, 1);
        assert_eq!(s.modules.member_count.max, 2);
        assert_eq!(s.bindings.total, 3);
        assert_eq!(s.bindings.renamed, 1);
        assert_eq!(s.bindings.unrenamed, 2);
        // `a` is in a singleton -> orphan; `b` and `c` share `pair` -> not.
        assert_eq!(s.bindings.orphan, 1);
    }

    #[test]
    fn empty_module_counts_and_min_zero() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(root, "empty.yaml", "members: []\n");
        write(
            root,
            "one.yaml",
            "members:\n  - selector: { binding: { name: a } }\n",
        );
        let s = compute_spec_stats(root).unwrap();
        assert_eq!(s.modules.total, 2);
        assert_eq!(s.modules.empty, 1);
        assert_eq!(s.modules.member_count.min, 0);
        assert_eq!(s.modules.member_count.max, 1);
        assert_eq!(s.modules.member_count.singletons, 1);
    }

    #[test]
    fn residual_module_counted() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(root, "residual/unhandled.yaml", "members: []\n");
        write(
            root,
            "ui/sidebar.yaml",
            "members:\n  - selector: { binding: { name: a } }\n",
        );
        let s = compute_spec_stats(root).unwrap();
        assert_eq!(s.modules.total, 2);
        assert_eq!(s.modules.residual, 1);
    }

    #[test]
    fn module_and_member_comments() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "annotated.yaml",
            "comment: header\nmembers:\n  - comment: per-member\n    selector: { binding: { name: a } }\n  - selector: { binding: { name: b } }\n",
        );
        let s = compute_spec_stats(root).unwrap();
        assert_eq!(s.modules.with_comment, 1);
        assert_eq!(s.bindings.with_comment, 1);
    }

    #[test]
    fn bucket_boundaries_singleton_tiny_medium_large() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "singleton.yaml",
            "members:\n  - selector: { binding: { name: a } }\n",
        );
        let tiny = (0..5)
            .map(|i| format!("  - selector: {{ binding: {{ name: t{i} }} }}\n"))
            .collect::<String>();
        write(root, "tiny.yaml", &format!("members:\n{tiny}"));
        let medium = (0..6)
            .map(|i| format!("  - selector: {{ binding: {{ name: m{i} }} }}\n"))
            .collect::<String>();
        write(root, "medium.yaml", &format!("members:\n{medium}"));
        let large = (0..21)
            .map(|i| format!("  - selector: {{ binding: {{ name: l{i} }} }}\n"))
            .collect::<String>();
        write(root, "large.yaml", &format!("members:\n{large}"));
        let s = compute_spec_stats(root).unwrap();
        assert_eq!(s.modules.member_count.singletons, 1);
        assert_eq!(s.modules.member_count.tiny_2_to_5, 1);
        assert_eq!(s.modules.member_count.medium_6_to_20, 1);
        assert_eq!(s.modules.member_count.large_21_plus, 1);
        assert_eq!(s.modules.member_count.max, 21);
    }

    #[test]
    fn deterministic_same_spec_same_output() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "a.yaml",
            "members:\n  - selector: { binding: { name: a } }\n  - selector: { binding: { name: b } }\n",
        );
        write(
            root,
            "b/c.yaml",
            "members:\n  - selector: { binding: { name: c } }\n",
        );
        let s1 = compute_spec_stats(root).unwrap();
        let s2 = compute_spec_stats(root).unwrap();
        let j1 = serde_json::to_string(&s1).unwrap();
        let j2 = serde_json::to_string(&s2).unwrap();
        assert_eq!(j1, j2);
    }
}
