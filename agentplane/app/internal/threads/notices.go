package threads

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

const MaxNoticePage = 100

type NoticeRevision struct {
	CommandID string
	Version   int64
}

type NoticeAcknowledgement struct {
	NoticeRevision
	ID             int64
	AcknowledgedAt time.Time
}

func (s *Store) AcknowledgeNotice(ctx context.Context, threadID uuid.UUID, notice NoticeRevision) error {
	_, err := s.db.ExecContext(ctx, `INSERT INTO command_notice_acknowledgement
		(thread_id,command_id,receipt_version) VALUES ($1,$2,$3)
		ON CONFLICT (thread_id,command_id,receipt_version) DO NOTHING`, threadID, notice.CommandID, notice.Version)
	return err
}

func (s *Store) FindNoticeAcknowledgements(ctx context.Context, threadID uuid.UUID, notices []NoticeRevision) ([]NoticeAcknowledgement, error) {
	if len(notices) == 0 || len(notices) > MaxNoticePage {
		return nil, fmt.Errorf("expected 1 to %d notice revisions", MaxNoticePage)
	}
	commands := make([]string, len(notices))
	versions := make([]int64, len(notices))
	for i, n := range notices {
		commands[i], versions[i] = n.CommandID, n.Version
	}
	rows, err := s.db.QueryContext(ctx, `SELECT acknowledgement_id,command_id,receipt_version,acknowledged_at
		FROM command_notice_acknowledgement WHERE thread_id=$1 AND (command_id,receipt_version) IN
		(SELECT * FROM unnest($2::text[], $3::bigint[]))`, threadID, pq.Array(commands), pq.Array(versions))
	if err != nil {
		return nil, err
	}
	return scanNoticeAcknowledgements(rows)
}

// ListNoticeAcknowledgements returns at most limit+1 records, newest insert first.
// The immutable ID distinguishes simultaneous acknowledgements across browser tabs.
func (s *Store) ListNoticeAcknowledgements(ctx context.Context, threadID uuid.UUID, before *int64, limit int) ([]NoticeAcknowledgement, error) {
	if limit < 1 || limit > MaxNoticePage {
		return nil, fmt.Errorf("expected a page of 1 to %d notices", MaxNoticePage)
	}
	query := `SELECT acknowledgement_id,command_id,receipt_version,acknowledged_at
		FROM command_notice_acknowledgement WHERE thread_id=$1`
	args := []any{threadID, limit + 1}
	if before != nil {
		query += " AND acknowledgement_id < $3"
		args = append(args, *before)
	}
	rows, err := s.db.QueryContext(ctx, query+" ORDER BY acknowledgement_id DESC LIMIT $2", args...)
	if err != nil {
		return nil, err
	}
	return scanNoticeAcknowledgements(rows)
}

func scanNoticeAcknowledgements(rows *sql.Rows) ([]NoticeAcknowledgement, error) {
	defer rows.Close()
	var records []NoticeAcknowledgement
	for rows.Next() {
		var record NoticeAcknowledgement
		if err := rows.Scan(&record.ID, &record.CommandID, &record.Version, &record.AcknowledgedAt); err != nil {
			return nil, err
		}
		records = append(records, record)
	}
	return records, rows.Err()
}
