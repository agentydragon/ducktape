package dispatcher

import (
	"testing"
	"time"

	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestSessionActivityReadAndWatch(t *testing.T) {
	store, databaseURL := testStoreWithURL(t, 3)
	ctx := admitted(t)
	hub := sessionWatchHub(t, databaseURL, "session-activity-"+uuid.NewString())
	service := sessionWatchService(store, hub)
	id := uuid.NewString()
	row, err := store.CreateSession(ctx, id, "test-activity", uuid.NewString(), &sessionpb.SessionSpec{}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	initial := unknownActivity(row, dispatcherpb.SessionActivity_Unknown_NOT_YET_OBSERVED)
	watch := startSessionWatch(t, service, ctx, "")
	assertRead := func(t *testing.T, want *dispatcherpb.SessionActivity) {
		t.Helper()
		got, err := service.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: id})
		if err != nil {
			t.Fatal(err)
		}
		listed, err := service.ListSessions(ctx, &dispatcherpb.ListSessionsRequest{})
		if err != nil {
			t.Fatal(err)
		}
		if len(listed.GetSessions()) != 1 {
			t.Fatalf("listed sessions = %v", listed)
		}
		watched := receiveSessionWatchFrame(t, watch).GetSession()
		for _, session := range []*dispatcherpb.Session{got.GetSession(), listed.GetSessions()[0], watched} {
			if !proto.Equal(session.GetStatus().GetActivity(), want) {
				t.Fatalf("activity = %v, want %v", session.GetStatus().GetActivity(), want)
			}
		}
	}
	assertRead(t, initial)
	receiveCaughtUp(t, watch)
	observed := &dispatcherpb.SessionActivity{
		RecordedAt: timestamppb.New(time.Now()),
		Kind: &dispatcherpb.SessionActivity_Observed{Observed: &harnesshostpb.Session{
			SessionId: id, ActiveTurnId: uuid.NewString(), PendingCommands: 2,
			Host: &harnesshostpb.Session_EnvironmentHosted{EnvironmentHosted: &harnesshostpb.EnvironmentHostedSession{
				Harness: &harnesshostpb.HarnessAttachment{State: harnesshostpb.HarnessAttachment_STATE_ATTACHED},
			}},
		}},
	}
	observedBytes, err := proto.Marshal(observed)
	if err != nil {
		t.Fatal(err)
	}
	// A rolling deployment can store fields this binary does not yet know.
	observedBytes = append(observedBytes, 0xa0, 0x06, 0x01)
	if err := proto.Unmarshal(observedBytes, observed); err != nil {
		t.Fatal(err)
	}
	suspended, err := proto.Marshal(&dispatcherpb.SessionActivity{
		RecordedAt: timestamppb.New(time.Now()),
		Kind:       &dispatcherpb.SessionActivity_EnvironmentDown_{EnvironmentDown: &dispatcherpb.SessionActivity_EnvironmentDown{Cause: dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED}},
	})
	if err != nil {
		t.Fatal(err)
	}
	deleted, err := proto.Marshal(&dispatcherpb.SessionActivity{
		RecordedAt: timestamppb.New(time.Now()),
		Kind:       &dispatcherpb.SessionActivity_EnvironmentDown_{EnvironmentDown: &dispatcherpb.SessionActivity_EnvironmentDown{Cause: dispatcherpb.SessionActivity_EnvironmentDown_DELETED}},
	})
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name  string
		bytes []byte
	}{
		{"observed with unknown fields", observedBytes},
		{"suspended", suspended},
		{"deleted", deleted},
		{"malformed", []byte{0xff}},
		{"future variant", []byte{0xa2, 0x06, 0x00}},
		{"empty protobuf", []byte{}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			if _, err := store.DB.ExecContext(ctx, "UPDATE session SET activity=$2 WHERE session_id=$1", id, test.bytes); err != nil {
				t.Fatal(err)
			}
			assertRead(t, unknownActivity(row, dispatcherpb.SessionActivity_Unknown_STREAM_LOST))
		})
	}
}
