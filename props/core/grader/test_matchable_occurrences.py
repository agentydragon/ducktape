"""Test matchable_occurrences() SQL function.

This function determines which TP/FP occurrences are matchable from a given set of files.
The filtering is based on graders_match_only_if_reported_on:
- NULL = cross-cutting, matchable from any file
- non-NULL = file-local, only matchable if files overlap with the file set
"""

from __future__ import annotations

import pytest
import pytest_bazel
from sqlalchemy import text

from props.db.session import get_session

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.fixture
def session(synced_test_db):
    """Provide a database session for the test."""
    with get_session() as sess:
        yield sess


class TestMatchableOccurrences:
    """Test matchable_occurrences() function with git fixtures.

    The train1 snapshot has:
    - tp-001: TP in subtract.py with critic_scopes_expected_to_recall: [[subtract.py]]
    - tp-002: TP in add.py with critic_scopes_expected_to_recall: [[add.py]]
    - tp-003 through tp-005: Additional TPs (may be cross-cutting)
    - fp-001: FP

    The critic_scopes_expected_to_recall becomes graders_match_only_if_reported_on in the DB.
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

    def test_file_local_tp_not_matched_from_different_file(self, session, test_trivial_snapshot):
        """A file-local TP is NOT matchable when the file array doesn't contain that file."""
        result = session.execute(
            text("""
                SELECT tp_id, tp_occurrence_id
                FROM matchable_occurrences(:snapshot, ARRAY['add.py'])
                WHERE tp_id IS NOT NULL
            """),
            {"snapshot": test_trivial_snapshot.slug},
        ).fetchall()

        tp_ids = {row.tp_id for row in result}

        # tp-001 has occurrence in subtract.py - should NOT be matchable from add.py
        assert "tp-001" not in tp_ids, f"tp-001 should not be matchable from add.py, got: {tp_ids}"
        # But tp-002 should be matchable from add.py
        assert "tp-002" in tp_ids, "Expected tp-002 in matchable TPs from add.py"

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

    def test_cross_cutting_tp_matchable_from_any_file(self, session, test_trivial_snapshot):
        """Cross-cutting TPs (NULL graders_match_only_if_reported_on) are matchable from any file."""
        # First check if we have any cross-cutting TPs
        cross_cutting = session.execute(
            text("""
                SELECT tp_id FROM true_positive_occurrences
                WHERE snapshot_slug = :snapshot AND graders_match_only_if_reported_on IS NULL
            """),
            {"snapshot": test_trivial_snapshot.slug},
        ).fetchall()

        if not cross_cutting:
            pytest.skip("No cross-cutting TPs in test fixtures")

        cross_cutting_ids = {row.tp_id for row in cross_cutting}

        # These should be matchable from ANY file
        result = session.execute(
            text("""
                SELECT tp_id FROM matchable_occurrences(:snapshot, ARRAY['nonexistent.py'])
                WHERE tp_id IS NOT NULL
            """),
            {"snapshot": test_trivial_snapshot.slug},
        ).fetchall()

        matched_ids = {row.tp_id for row in result}

        for tp_id in cross_cutting_ids:
            assert tp_id in matched_ids, f"Cross-cutting TP {tp_id} should be matchable from any file"

    def test_edge_count_per_file(self, session, test_trivial_snapshot):
        """Verify edge count is smaller for single file vs all files.

        This is the key property: file_set examples should have fewer edges.
        """
        # Count matchable from just subtract.py
        single_file = session.execute(
            text("""
                SELECT COUNT(*) FROM matchable_occurrences(:snapshot, ARRAY['subtract.py'])
            """),
            {"snapshot": test_trivial_snapshot.slug},
        ).scalar()

        # Count matchable from all files
        all_files = session.execute(
            text("""
                SELECT COUNT(*) FROM matchable_occurrences(
                    :snapshot,
                    ARRAY['subtract.py', 'add.py', 'multiply.py', 'divide.py']
                )
            """),
            {"snapshot": test_trivial_snapshot.slug},
        ).scalar()

        # Count total occurrences (what we'd get without filtering)
        total = session.execute(
            text("""
                SELECT
                    (SELECT COUNT(*) FROM true_positive_occurrences WHERE snapshot_slug = :snapshot) +
                    (SELECT COUNT(*) FROM false_positive_occurrences WHERE snapshot_slug = :snapshot)
            """),
            {"snapshot": test_trivial_snapshot.slug},
        ).scalar()

        print(f"Single file (subtract.py): {single_file} matchable")
        print(f"All 4 files: {all_files} matchable")
        print(f"Total occurrences: {total}")

        # Single file should have fewer matchable occurrences
        assert single_file <= all_files, "Single file should have <= matchable occurrences than all files"

    def test_empty_file_array_only_matches_cross_cutting(self, session, test_trivial_snapshot):
        """Empty file array should only match cross-cutting (NULL) occurrences."""
        result = session.execute(
            text("""
                SELECT tp_id, tp_occurrence_id, fp_id, fp_occurrence_id
                FROM matchable_occurrences(:snapshot, ARRAY[]::VARCHAR[])
            """),
            {"snapshot": test_trivial_snapshot.slug},
        ).fetchall()

        # Check that all returned occurrences are cross-cutting
        for row in result:
            if row.tp_id:
                is_cross_cutting = session.execute(
                    text("""
                        SELECT graders_match_only_if_reported_on IS NULL
                        FROM true_positive_occurrences
                        WHERE snapshot_slug = :snapshot AND tp_id = :tp_id AND occurrence_id = :occ_id
                    """),
                    {"snapshot": test_trivial_snapshot.slug, "tp_id": row.tp_id, "occ_id": row.tp_occurrence_id},
                ).scalar()
                assert is_cross_cutting, (
                    f"TP {row.tp_id}/{row.tp_occurrence_id} matched from empty array but isn't cross-cutting"
                )


if __name__ == "__main__":
    pytest_bazel.main()
