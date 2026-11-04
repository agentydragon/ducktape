package migrations

import (
	"bytes"
	"database/sql"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func TestLegacySamplerSummaryRepairPreservesFeedAndRejectsMismatch(t *testing.T) {
	for _, test := range []struct {
		name, service string
		wantError     bool
	}{
		{"matching retained service", "fixture-service", false},
		{"mismatched retained service", "different-service", true},
	} {
		t.Run(test.name, func(t *testing.T) {
			db, err := sql.Open("postgres", migrationDatabase.Clone(t))
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = db.Close() })
			provider, err := newProvider(db)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := provider.UpTo(t.Context(), 9); err != nil {
				t.Fatal(err)
			}
			const id = "00000000-0000-4000-8000-000000000810"
			oldConfig := protowire.AppendString(protowire.AppendTag(nil, 1, protowire.BytesType), "fixture-service")
			spec := &sessionpb.SessionSpec{Cwd: "/fixture"}
			spec.ProtoReflect().SetUnknown(protowire.AppendBytes(protowire.AppendTag(nil, 6, protowire.BytesType), oldConfig))
			feed := &historypb.SessionFeedState{Attached: &sessionpb.Attached{SessionId: id, Spec: spec}}
			original, err := proto.Marshal(feed)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := db.ExecContext(t.Context(), `INSERT INTO session_history(id,last_cursor,feed_state) VALUES($1,0,$2)`, id, original); err != nil {
				t.Fatal(err)
			}
			if _, err := db.ExecContext(t.Context(), `INSERT INTO thread_history_summary(thread_id,attached) VALUES($1,jsonb_build_object('sessionId',$1::uuid::text,'spec',jsonb_build_object('cwd','/fixture','centralSampler',jsonb_build_object('service',$2::text))))`, id, test.service); err != nil {
				t.Fatal(err)
			}
			_, err = provider.Up(t.Context())
			if (err != nil) != test.wantError {
				t.Fatalf("migration error = %v, want error %v", err, test.wantError)
			}
			var feedAfter, summary []byte
			if err := db.QueryRowContext(t.Context(), `SELECT h.feed_state,s.attached FROM session_history h JOIN thread_history_summary s ON h.id=s.thread_id WHERE h.id=$1`, id).Scan(&feedAfter, &summary); err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(feedAfter, original) {
				t.Fatal("retained feed changed")
			}
			if test.wantError {
				if !bytes.Contains(summary, []byte("centralSampler")) {
					t.Fatal("failed migration removed the original field")
				}
				return
			}
			var attached sessionpb.Attached
			if err := protojson.Unmarshal(summary, &attached); err != nil {
				t.Fatalf("repaired summary is unreadable: %v", err)
			}
			if attached.GetSessionId() != id || attached.GetSpec().GetCwd() != "/fixture" {
				t.Fatalf("lost retained summary: %v", &attached)
			}
		})
	}
}
