package threads

import (
	"errors"
	"testing"

	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/protobuf/proto"
)

func TestEnsureThreadArchivableObservedActivity(t *testing.T) {
	const sessionID = "test-session"
	for _, host := range []string{"hosted attached", "hosted detached", "central"} {
		for _, test := range []struct {
			name       string
			setup      sessionpb.SetupState
			activeTurn string
			pending    uint32
			want       error
		}{
			{name: "idle", setup: sessionpb.SetupState_SETUP_STATE_NOT_REQUIRED},
			{name: "setup succeeded", setup: sessionpb.SetupState_SETUP_STATE_SUCCEEDED},
			{name: "setup failed", setup: sessionpb.SetupState_SETUP_STATE_FAILED},
			{name: "setup interrupted", setup: sessionpb.SetupState_SETUP_STATE_INTERRUPTED},
			{name: "active turn", setup: sessionpb.SetupState_SETUP_STATE_SUCCEEDED, activeTurn: "test-turn", want: errThreadBusy},
			{name: "pending command", setup: sessionpb.SetupState_SETUP_STATE_NOT_REQUIRED, pending: 1, want: errThreadBusy},
			{name: "setup running", setup: sessionpb.SetupState_SETUP_STATE_RUNNING, want: errThreadBusy},
			{name: "overlapping work", setup: sessionpb.SetupState_SETUP_STATE_RUNNING, activeTurn: "test-turn", pending: 2, want: errThreadBusy},
			{name: "setup unspecified", want: errThreadActivityUnknown},
			{name: "future setup", setup: sessionpb.SetupState(999), want: errThreadActivityUnknown},
		} {
			t.Run(host+"/"+test.name, func(t *testing.T) {
				observed := &harnesshostpb.Session{SessionId: sessionID, ActiveTurnId: test.activeTurn, PendingCommands: test.pending}
				if host == "central" {
					observed.Host = &harnesshostpb.Session_CentralSamplerV1{CentralSamplerV1: &harnesshostpb.CentralSamplerSession{Setup: test.setup}}
				} else {
					state := harnesshostpb.HarnessAttachment_STATE_ATTACHED
					if host == "hosted detached" {
						state = harnesshostpb.HarnessAttachment_STATE_DETACHED
					}
					observed.Host = &harnesshostpb.Session_EnvironmentHosted{EnvironmentHosted: &harnesshostpb.EnvironmentHostedSession{Setup: test.setup, Harness: &harnesshostpb.HarnessAttachment{State: state}}}
				}
				activity := &dispatcherpb.SessionActivity{Kind: &dispatcherpb.SessionActivity_Observed{Observed: observed}}
				if err := ensureThreadArchivable(sessionID, activity); !errors.Is(err, test.want) {
					t.Fatalf("archive check = %v, want %v", err, test.want)
				}
			})
		}
	}
}

func TestEnsureThreadArchivableRejectsUncertainActivity(t *testing.T) {
	const sessionID = "test-session"
	unknownVariant := &dispatcherpb.SessionActivity{}
	unknownVariant.ProtoReflect().SetUnknown([]byte{0x2a, 0x00}) // A future oneof field 5.
	for _, activity := range []*dispatcherpb.SessionActivity{
		nil,
		{},
		unknownVariant,
		{Kind: &dispatcherpb.SessionActivity_Unknown_{Unknown: &dispatcherpb.SessionActivity_Unknown{Reason: dispatcherpb.SessionActivity_Unknown_NOT_YET_OBSERVED}}},
		{Kind: &dispatcherpb.SessionActivity_Unknown_{Unknown: &dispatcherpb.SessionActivity_Unknown{Reason: dispatcherpb.SessionActivity_Unknown_STREAM_LOST}}},
		{Kind: &dispatcherpb.SessionActivity_Observed{}},
		{Kind: &dispatcherpb.SessionActivity_Observed{Observed: &harnesshostpb.Session{SessionId: sessionID}}},
		{Kind: &dispatcherpb.SessionActivity_Observed{Observed: &harnesshostpb.Session{SessionId: sessionID, Host: &harnesshostpb.Session_EnvironmentHosted{}}}},
		{Kind: &dispatcherpb.SessionActivity_Observed{Observed: &harnesshostpb.Session{SessionId: sessionID, Host: &harnesshostpb.Session_CentralSamplerV1{}}}},
		{Kind: &dispatcherpb.SessionActivity_EnvironmentDown_{}},
		{Kind: &dispatcherpb.SessionActivity_EnvironmentDown_{EnvironmentDown: &dispatcherpb.SessionActivity_EnvironmentDown{Cause: dispatcherpb.SessionActivity_EnvironmentDown_Cause(999)}}},
	} {
		if err := ensureThreadArchivable(sessionID, activity); !errors.Is(err, errThreadActivityUnknown) {
			t.Errorf("archive check for %v = %v, want unknown", activity, err)
		}
	}
	for _, state := range []harnesshostpb.HarnessAttachment_State{harnesshostpb.HarnessAttachment_STATE_UNSPECIFIED, harnesshostpb.HarnessAttachment_State(999), harnesshostpb.HarnessAttachment_STATE_STARTING} {
		activity := &dispatcherpb.SessionActivity{Kind: &dispatcherpb.SessionActivity_Observed{Observed: &harnesshostpb.Session{SessionId: sessionID, Host: &harnesshostpb.Session_EnvironmentHosted{EnvironmentHosted: &harnesshostpb.EnvironmentHostedSession{Setup: sessionpb.SetupState_SETUP_STATE_SUCCEEDED, Harness: &harnesshostpb.HarnessAttachment{State: state}}}}}}
		want := errThreadActivityUnknown
		if state == harnesshostpb.HarnessAttachment_STATE_STARTING {
			want = errThreadBusy
		}
		if err := ensureThreadArchivable(sessionID, activity); !errors.Is(err, want) {
			t.Errorf("attachment %v = %v, want %v", state, err, want)
		}
		mismatched := proto.Clone(activity).(*dispatcherpb.SessionActivity)
		mismatched.GetObserved().SessionId = "test-other-session"
		if err := ensureThreadArchivable(sessionID, mismatched); !errors.Is(err, errThreadActivityUnknown) {
			t.Errorf("mismatched activity = %v, want unknown", err)
		}
	}
}

func TestEnsureThreadArchivableAllowsConfirmedEnvironmentDown(t *testing.T) {
	for _, cause := range []dispatcherpb.SessionActivity_EnvironmentDown_Cause{dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED, dispatcherpb.SessionActivity_EnvironmentDown_DELETED} {
		activity := &dispatcherpb.SessionActivity{Kind: &dispatcherpb.SessionActivity_EnvironmentDown_{EnvironmentDown: &dispatcherpb.SessionActivity_EnvironmentDown{Cause: cause}}}
		if err := ensureThreadArchivable("test-session", activity); err != nil {
			t.Errorf("confirmed %v archive check = %v", cause, err)
		}
	}
}
