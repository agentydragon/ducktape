package historytesting

import (
	"database/sql"
	"encoding/json"
	"strings"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/google/uuid"
)

// PayloadText reads precisely the ordered chunks covered by this stored reference.
func PayloadText(t testing.TB, db *sql.DB, threadID uuid.UUID, reference *historypb.ThreadPayloadReference, field string) string {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), `SELECT text FROM thread_payload_chunk WHERE thread_id = $1 AND projection_epoch = $2
		AND owner_cursor = $3 AND owner_id = $4 AND field = $5 AND generation = $6 AND chunk_index < $7
		ORDER BY chunk_index`, threadID, reference.ProjectionEpoch, reference.OwnerCursor, reference.OwnerId,
		field, reference.Generation, reference.ChunkCount)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var body strings.Builder
	for rows.Next() {
		var encoded []byte
		var chunk string
		if err := rows.Scan(&encoded); err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(encoded, &chunk); err != nil {
			t.Fatal(err)
		}
		body.WriteString(chunk)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return body.String()
}
