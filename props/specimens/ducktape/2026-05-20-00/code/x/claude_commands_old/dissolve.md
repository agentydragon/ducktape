# Dissolve

## Description

Merge duplicated functionality into a single deduplicated version. This applies to any duplicated content - code, documentation, configuration, etc. The core principle is consolidating multiple versions/variants that arose from copy-paste-modify patterns into a unified, feature-complete version.

## Instructions

### 1. Identify Duplication Pattern

- Find files with similar names/purposes (e.g., `plot_weight.py`, `plot_weight_over_time_v2.py`)
- Identify the copy-paste-modify pattern
- Determine which version has the most complete feature set

### 2. Analyze Feature Differences

- List features unique to each variant
- Identify common core functionality
- Note parameter differences, output formats, etc.
- Determine the "best" base version (usually the most recent/feature-rich)

### 3. Plan Consolidation

- Choose target filename (often close to the most advanced version)
- Map how to merge unique features from each variant
- Design unified interface that encompasses all use cases
- Plan parameter handling for different modes/options

### 4. Universal Dissolution Strategy

1. **Start with most complete variant** - Identify which version has the richest feature set
2. **Extract unique additions** - Identify what each variant adds beyond the base
3. **Design unified interface** - Create a way to access all functionality through configuration/parameters/profiles
4. **Merge without loss** - Ensure every capability from every variant is preserved
5. **Make variants accessible** - Use flags/modes/profiles to enable variant-specific behavior

**Examples across domains:**

**Code**: `analyze_cluster.py`, `analyze_cluster_deep.py`, `analyze_cluster_with_viz.py`
→ Single `analyze_cluster.py --deep --visualize`

**Documentation**: `README.md`, `README-DEV.md`, `README-ADVANCED.md`
→ Single `README.md` with sections: "Getting Started", "Development", "Advanced Usage"

**Configuration**: `docker-compose.yml`, `docker-compose-dev.yml`, `docker-compose-prod.yml`
→ Single file with profiles: `docker compose --profile dev up`

**CI/CD**: `test.yml`, `test-and-lint.yml`, `full-ci.yml`, `deploy.yml`
→ Single workflow with conditional jobs based on triggers/inputs

**Data**: `users.json`, `users-with-roles.json`, `users-full-export-v3.json`
→ Single schema with optional fields and export levels

The pattern is always the same: consolidate into one artifact that can express all variants through configuration.

### 5. Always present your plan before making changes

```
Those 3 scripts all execute different clustering algorithms on our dataset:

• analyze_cluster.py (156 lines, modified yesterday)
• analyze_cluster_deep.py (289 lines, modified 3 days ago)
• analyze_cluster_with_viz.py (412 lines, modified today)

It looks like analyze_cluster_with_viz.py is newest and most feature-rich,
so I'll use it as the baseline.

Here's what each variant adds:
- analyze_cluster.py: Just the basic cluster analysis
- analyze_cluster_deep.py: Adds recursive analysis and metrics
- analyze_cluster_with_viz.py: Has everything above plus visualization

I recommend merging them into one analyze_cluster.py with optional flags:
- Default behavior matching original analyze_cluster.py
- --deep for recursive analysis
- --visualize for generating graphs

This would change the interface slightly - instead of choosing different scripts,
you'd use flags. Current calls to analyze_cluster_deep.py would become
analyze_cluster.py --deep.

OK to proceed? Should I delete the old scripts after merging, or archive tehem?
```

### 6. Execution

After user approves:

1. Create the consolidated file (keep originals during testing)
2. If applicable, test that consolidated file covers all original functions
3. Update any references to point to new file
4. Archive or delete the now-redundant variants
5. As appropriate (e.g., if consolidating production tools rather than one-off personal
   experiments), document the consolidation clearly

## Example Workflows

### Code

```bash
# Found similar files:
# - plot_weight.py (150 lines) - Basic weight plotting
# - plot_weight_over_time.py (200 lines) - Adds time series
# - plot_weight_stats_v2.py (350 lines) - Adds statistics
# → Dissolve into: plot_weight.py with --stats and --time flags
```

### Documentation

```bash
Found duplicated guides:
- README.md (500 lines) - Original getting started
- SETUP.md (200 lines) - Detailed setup steps
- QUICKSTART.md (100 lines) - Condensed version
→ Dissolve into: docs/getting-started.md with all content organized
```

### Configuration

```bash
Found workflow files:
- .github/workflows/test.yml - Runs tests
- .github/workflows/test-and-lint.yml - Tests + linting
- .github/workflows/ci-complete.yml - Tests + lint + build
- .github/workflows/ci-final-v3.yml - All above + deploy
→ Dissolve into: .github/workflows/ci.yml with job conditions
```

## Principles

- Always preserve valuable information
- Improve organization while moving content
- Document the dissolution process
- Ensure no broken references
- Prefer existing homes over creating new files
- Be aggressive about removing true duplicates
- Be conservative about deleting unique content

## Common Dissolution Targets

- Multiple versions of similar scripts (v1, v2, final, FINAL-FINAL)
- Copy-pasted-modified analysis scripts
- Similar plotting/visualization tools with slight variations
- Docker compose files for different environments
- CI/CD workflows with incremental additions
- Configuration files with overlapping settings
- Test files that were duplicated and extended
- README/SETUP/QUICKSTART documentation sprawl

## Output Format

Provide a dissolution report:

```
## Dissolution report

4 files dissolved:

- analyze_cluster_deep.py
- analyze_cluster_with_viz.py
- writeups/clusters2.md
- writeups/clusters-final.md

Into:

- analyze_cluster.py (including recursive analysis and visualization)
- writeups/clusters.md (consolidating all cluster analysis content)

## Steps

- I updated analyze_cluster.py:
  - I added a --deep option for recursive analysis (to cover analyze_cluster_deep.py),
  - I added a --visualize option for generating graphs (to cover analyze_cluster_with_viz.py).
  - Otherwise it behaves as before analyze_cluster.py.
- Then I updated one reference:
  - File docstring in full_pipeline.py listing analyze_cluster_with_viz.py as optional viz step
- Then I merged findings from clusters2.md and clusters-final.md into clusters.md.
- Finally, I deleted the old scripts and writeups:
  - analyze_cluster_deep.py
  - analyze_cluster_with_viz.py
  - writeups/clusters2.md
  - writeups/clusters-final.md
- I did not keep the --json option on analyze_cluster_deep.py, since it appears unused
  and would be easy to re-add if needed.

## Not dissolved

I did not edit analyse_cluster_static.py - it loads a different dataset and
seems to be from a different set of experiments.
```
