# Grading Schema

## Bipartite Edge Model

Grading fills a bipartite graph between critique issues (left) and ground truth occurrences (right). **Every** (issue, matchable_occurrence) pair needs an edge:

| Critique Issue | GT Occurrence | Credit | Rationale |
|---------------|---------------|--------|-----------|
| input-001 | tp/dead-code/occ-0 | 1.0 | Exact match |
| input-001 | tp/security/occ-0 | 0.0 | Not related |
| input-001 | fp/known-pattern/occ-0 | 0.0 | Not triggered |
| input-002 | tp/dead-code/occ-0 | 0.5 | Partial match |

Each edge carries:
- **credit** (0.0-1.0): How well the issue matches the occurrence (0.0 = no match, 1.0 = perfect match)
- **rationale**: Your reasoning

**Sum constraint:** Each occurrence can receive at most 1.0 total credit across all issues.

${describe_relation("grading_edges")}

## Pending Edges (Completeness)

${describe_relation("grading_pending")}

**Source of truth for completeness:** Query `grading_pending` to see missing edges. Grading is complete when no rows remain. The grader `sleep` tool validates this and rejects sleep if any edges are pending.

## Sparse Matching (match_file_restriction)

Not all occurrences are matchable from all critique issues. The `match_file_restriction` field on TP/FP occurrences controls which critiques can create edges to them:

| `match_file_restriction` | Meaning | Who can match |
|--------------------------|---------|---------------|
| null/omitted | Unrestricted | Any critique can match, regardless of files touched |
| `["file1.py", "file2.py"]` | File-restricted | Only critiques that touched at least one file in that set |

The `grading_pending` view automatically enforces this - it only shows (issue, occurrence) pairs where the critique's files overlap.

## Issue Clustering

After grading is complete, issues with no positive match (credit=0 for all GT) need clustering. Clusters group independent reports of the same novel finding across critic runs.

${describe_relation("issue_clusters")}
${describe_relation("issue_cluster_members")}

### Clustering Pending (Completeness)

${describe_relation("clustering_pending")}

**Source of truth for clustering completeness:** Query `clustering_pending` to see unclustered issues. Clustering is complete when no rows remain. The `sleep` tool validates both `grading_pending` and `clustering_pending` are empty.

### Invariants

- **Exclusivity**: An issue with any grading edge with credit > 0 cannot be in a cluster (DB trigger enforced)
- **Reverse guard**: A grading edge with credit > 0 cannot be inserted for an already-clustered issue (DB trigger enforced)
- **Single cluster**: Each critique issue belongs to at most one cluster (UNIQUE constraint)
- **Non-empty clusters**: Removing the last member from a cluster is rejected — delete the cluster instead (deferred trigger)
