package migrations

import (
	"bytes"
	"database/sql"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/reads"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func TestSessionPlacementMigrationPreservesArchivedHistoryWithoutInventingEnvironment(t *testing.T) {
	db, err := sql.Open("postgres", migrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	provider, err := legacyProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 7); err != nil {
		t.Fatal(err)
	}
	const id = "00000000-0000-4000-8000-000000000807"
	specUnknown := protowire.AppendVarint(protowire.AppendTag(nil, 1, protowire.VarintType), uint64(sessionpb.Harness_HARNESS_CLAUDE))
	future := protowire.AppendBytes(protowire.AppendTag(nil, 99, protowire.BytesType), []byte("future-spec"))
	specUnknown = append(specUnknown, future...)
	spec := &sessionpb.SessionSpec{Cwd: "/old-workspace"}
	spec.ProtoReflect().SetUnknown(specUnknown)
	attached := &sessionpb.Attached{SessionId: id, Spec: spec}
	attached.ProtoReflect().SetUnknown(protowire.AppendVarint(protowire.AppendTag(nil, 98, protowire.VarintType), 7))
	feed := &historypb.SessionFeedState{Attached: attached}
	feed.ProtoReflect().SetUnknown(protowire.AppendVarint(protowire.AppendTag(nil, 97, protowire.VarintType), 9))
	feedRaw, err := proto.Marshal(feed)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_history(id,last_cursor,feed_state) VALUES($1,2,$2)`, id, feedRaw); err != nil {
		t.Fatal(err)
	}
	archived := `{"sessionId":"` + id + `","spec":{"harness":"HARNESS_CLAUDE","cwd":"/old-workspace"}}`
	if _, err := db.ExecContext(t.Context(), `INSERT INTO thread_history_summary(thread_id,attached) VALUES($1,$2::jsonb)`, id, archived); err != nil {
		t.Fatal(err)
	}
	first, second := []byte{0x08, 0x01}, []byte{0x08, 0x02}
	for cursor, payload := range [][]byte{first, second} {
		if _, err := db.ExecContext(t.Context(), `INSERT INTO session_event(session_id,cursor,payload,kind) VALUES($1,$2,$3,0)`, id, cursor+1, payload); err != nil {
			t.Fatal(err)
		}
	}
	var before int64
	if err := db.QueryRowContext(t.Context(), `SELECT version FROM session_fold_pending WHERE session_id=$1`, id).Scan(&before); err != nil {
		t.Fatal(err)
	}
	if _, err := provider.Up(t.Context()); err != nil {
		t.Fatal(err)
	}
	var converted []byte
	var after int64
	if err := db.QueryRowContext(t.Context(), `SELECT h.feed_state,p.version FROM session_history h JOIN session_fold_pending p ON p.session_id=h.id WHERE h.id=$1`, id).Scan(&converted, &after); err != nil {
		t.Fatal(err)
	}
	if after <= before {
		t.Fatalf("refold version = %d, want newer than %d", after, before)
	}
	var read historypb.SessionFeedState
	if err := proto.Unmarshal(converted, &read); err != nil {
		t.Fatal(err)
	}
	if read.GetAttached().GetHistoricalHarness() != sessionpb.Harness_HARNESS_CLAUDE || read.GetAttached().GetSpec().GetPlacement() != nil ||
		!bytes.Equal(read.GetAttached().GetSpec().ProtoReflect().GetUnknown(), future) ||
		!bytes.Equal(read.GetAttached().ProtoReflect().GetUnknown(), attached.ProtoReflect().GetUnknown()) ||
		!bytes.Equal(read.ProtoReflect().GetUnknown(), feed.ProtoReflect().GetUnknown()) {
		t.Fatalf("archived feed was not preserved: %v", &read)
	}
	var summary []byte
	if err := db.QueryRowContext(t.Context(), `SELECT attached FROM thread_history_summary WHERE thread_id=$1`, id).Scan(&summary); err != nil {
		t.Fatal(err)
	}
	var readAttached sessionpb.Attached
	if err := protojson.Unmarshal(summary, &readAttached); err != nil {
		t.Fatal(err)
	}
	if readAttached.GetHistoricalHarness() != sessionpb.Harness_HARNESS_CLAUDE || readAttached.GetSpec().GetPlacement() != nil {
		t.Fatalf("archived summary fabricated placement or lost harness: %v", &readAttached)
	}
	rows, err := db.QueryContext(t.Context(), `SELECT cursor,payload FROM session_event WHERE session_id=$1 ORDER BY cursor`, id)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	for index, want := range [][]byte{first, second} {
		if !rows.Next() {
			t.Fatalf("missing archived event %d", index+1)
		}
		var cursor int
		var payload []byte
		if err := rows.Scan(&cursor, &payload); err != nil {
			t.Fatal(err)
		}
		if cursor != index+1 || !bytes.Equal(payload, want) {
			t.Fatalf("archived event %d = %d %x", index+1, cursor, payload)
		}
	}
	if rows.Next() {
		t.Fatal("unexpected archived event")
	}
}

func TestSessionPlacementMigrationAllowsSameCursorRetainedAnnouncement(t *testing.T) {
	for _, test := range []struct {
		name    string
		harness sessionpb.Harness
	}{
		{"native", sessionpb.Harness_HARNESS_CODEX},
		{"central", sessionpb.Harness_HARNESS_CENTRAL_SAMPLER},
	} {
		t.Run(test.name, func(t *testing.T) {
			db, err := sql.Open("postgres", migrationDatabase.Clone(t))
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = db.Close() })
			provider, err := legacyProvider(db)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := provider.UpTo(t.Context(), 7); err != nil {
				t.Fatal(err)
			}
			id, targetUID := uuid.New(), uuid.NewString()
			spec := &sessionpb.SessionSpec{Cwd: "/retained"}
			spec.ProtoReflect().SetUnknown(protowire.AppendVarint(protowire.AppendTag(nil, 1, protowire.VarintType), uint64(test.harness)))
			feedRaw, err := proto.Marshal(&historypb.SessionFeedState{Attached: &sessionpb.Attached{SessionId: id.String(), Spec: spec}})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := db.ExecContext(t.Context(), `INSERT INTO session_history(id,last_cursor,feed_state,sandbox_namespace,sandbox_name,sandbox_uid)
				VALUES($1,0,$2,'agents','executor',$3)`, id, feedRaw, targetUID); err != nil {
				t.Fatal(err)
			}
			attachedJSON := `{"sessionId":"` + id.String() + `","spec":{"harness":"` + test.harness.String() + `","cwd":"/retained"}}`
			if _, err := db.ExecContext(t.Context(), `INSERT INTO thread_history_summary(thread_id,attached) VALUES($1,$2::jsonb)`, id, attachedJSON); err != nil {
				t.Fatal(err)
			}
			if _, err := provider.Up(t.Context()); err != nil {
				t.Fatal(err)
			}
			fresh := &sessionpb.SessionSpec{Cwd: "/retained"}
			target := &sessionpb.ExactEnvironment{Name: "executor", Uid: targetUID}
			if test.harness == sessionpb.Harness_HARNESS_CENTRAL_SAMPLER {
				fresh.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: target}}}
			} else {
				fresh.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: target, Harness: test.harness}}}
			}
			var feedWire, summaryWire []byte
			if err := db.QueryRowContext(t.Context(), `SELECT feed_state FROM session_history WHERE id=$1`, id).Scan(&feedWire); err != nil {
				t.Fatal(err)
			}
			if err := db.QueryRowContext(t.Context(), `SELECT attached FROM thread_history_summary WHERE thread_id=$1`, id).Scan(&summaryWire); err != nil {
				t.Fatal(err)
			}
			var feed historypb.SessionFeedState
			if err := proto.Unmarshal(feedWire, &feed); err != nil {
				t.Fatal(err)
			}
			var summary sessionpb.Attached
			if err := protojson.Unmarshal(summaryWire, &summary); err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(feed.GetAttached().GetSpec(), fresh) || !proto.Equal(summary.GetSpec(), fresh) ||
				feed.GetAttached().GetHistoricalHarness() != sessionpb.Harness_HARNESS_UNSPECIFIED || summary.GetHistoricalHarness() != sessionpb.Harness_HARNESS_UNSPECIFIED {
				t.Fatalf("retained %s placement differs between feed and summary: %v %v", test.name, &feed, &summary)
			}
			visible, err := (&reads.Reader{DB: db, History: storage.NewStore(db, true)}).GetSessionSummary(t.Context(),
				&historypb.GetSessionSummaryRequest{SessionId: id.String()})
			if err != nil {
				t.Fatalf("read migrated summary: %v", err)
			}
			if !proto.Equal(visible.GetAttached().GetSpec().GetPlacement(), fresh.GetPlacement()) {
				t.Fatalf("read summary placement = %v, want %v", visible.GetAttached().GetSpec().GetPlacement(), fresh.GetPlacement())
			}
			// The retained writer can reannounce at its existing cursor after the coordinated cutover.
			if err := storage.NewStore(db, true).RecordFeedState(t.Context(), id, &historypb.SessionFeedState{Attached: &sessionpb.Attached{SessionId: id.String(), Spec: fresh}}); err != nil {
				t.Fatalf("same-cursor retained announcement: %v", err)
			}
		})
	}
}
