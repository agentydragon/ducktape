"""Test matchable_occurrences() SQL function.

This function determines which TP/FP occurrences are matchable from a given set of files.
The filtering is based on match_file_restriction:
- NULL = unrestricted, matchable from any file
- non-NULL = file-restricted, only matchable if files overlap with the file set
"""

from __future__ import annotations

import pytest_bazel
from sqlalchemy import text


class TestMatchableOccurrences:
    """Test matchable_occurrences() function with git fixtures.

    The train1 snapshot has:
    - tp-001: TP in subtract.py with critic_scopes_expected_to_recall: [[subtract.py]]
    - tp-002: TP in add.py with critic_scopes_expected_to_recall: [[add.py]]
    - tp-003 through tp-005: Additional TPs (may be unrestricted)
    - fp-001: FP

    The critic_scopes_expected_to_recall becomes match_file_restriction in the DB.
    """

    def test_file_local_tp_matched_from_same_file(self, session, test_trivial_snapshot):
        """A file-local TP is matchable when the file array contains that file."""
        result = session.execute(
            text("""
                SELECT tp_id, tp_occurrence_id, fp_id, fp_occurrence_id
                FROM matchable_occurrences(:snapshot, ARRAY['subtract.py'])
                WHERE tp_id IS NOT NULL
            """),
            {"snapshot": test_trivial_snapshot.slug},
        ).fetchall()

        tp_ids = {row.tp_id for row in result}

        # tp-001 has occurrence in subtract.py with critic_scopes_expected_to_recall: [[subtract.py]]
        assert "tp-001" in tp_ids, f"Expected tp-001 in matchable TPs, got: {tp_ids}"

    def test_multiple_files_match_their_local_tps(self, session, test_trivial_snapshot):
        """Multiple files should match their respective file-local TPs."""
        result = session.execute(
            text("""
                SELECT tp_id, tp_occurrence_id
                FROM matchable_occurrences(:snapshot, ARRAY['subtract.py', 'add.py'])
                WHERE tp_id IS NOT NULL
            """),
            {"snapshot": test_trivial_snapshot.slug},
        ).fetchall()

        tp_ids = {row.tp_id for row in result}

        # Both should be matchable
        assert "tp-001" in tp_ids, "tp-001 should be matchable from {subtract.py, add.py}"
        assert "tp-002" in tp_ids, "tp-002 should be matchable from {subtract.py, add.py}"

    def test_empty_file_array_only_matches_unrestricted(self, session, test_trivial_snapshot):
        """Empty file array should only match unrestricted (NULL) occurrences."""
        result = session.execute(
            text("""
                SELECT tp_id, tp_occurrence_id, fp_id, fp_occurrence_id
                FROM matchable_occurrences(:snapshot, ARRAY[]::VARCHAR[])
            """),
            {"snapshot": test_trivial_snapshot.slug},
        ).fetchall()

        # Check that all returned occurrences are unrestricted
        for row in result:
            if row.tp_id:
                is_unrestricted = session.execute(
                    text("""
                        SELECT match_file_restriction IS NULL
                        FROM true_positive_occurrences
                        WHERE snapshot_slug = :snapshot AND tp_id = :tp_id AND occurrence_id = :occ_id
                    """),
                    {"snapshot": test_trivial_snapshot.slug, "tp_id": row.tp_id, "occ_id": row.tp_occurrence_id},
                ).scalar()
                assert is_unrestricted, (
                    f"TP {row.tp_id}/{row.tp_occurrence_id} matched from empty array but isn't unrestricted"
                )


if __name__ == "__main__":
    pytest_bazel.main()
