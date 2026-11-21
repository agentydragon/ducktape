package sessionstore

import (
	"errors"
	"strings"
	"testing"
	"time"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func TestHostSessionsTracksAdmissionAndSetup(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	admitInput(t, store, session.ID, "input", "hello")
	sessions, err := store.HostSessions(t.Context(), "fixture-account")
	if err != nil || len(sessions) != 1 {
		t.Fatalf("sessions=%v err=%v", sessions, err)
	}
	if got := sessions[0]; got.GetSessionId() != session.ID || got.GetActiveTurnId() != "" || got.GetPendingCommands() != 1 || got.GetCentralSamplerV1().GetSetup() != sessionpb.SetupState_SETUP_STATE_NOT_REQUIRED || got.GetCentralSamplerV1().GetBlocked() != nil {
		t.Fatalf("admitted status=%v", got)
	}
	hidden, err := store.HostSessions(t.Context(), "other-account")
	if err != nil || len(hidden) != 0 {
		t.Fatalf("other owner's sessions=%v err=%v", hidden, err)
	}
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
		t.Fatal(err)
	}
	sessions, err = store.HostSessions(t.Context(), "fixture-account")
	if err != nil || sessions[0].GetActiveTurnId() != "input" || sessions[0].GetPendingCommands() != 0 {
		t.Fatalf("active sessions=%v err=%v", sessions, err)
	}
	setup := openSessionForStore(t, store, "echo setup")
	setupOwner, err := store.Claim(t.Context(), setup.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.StartSetup(t.Context(), setupOwner, setup.ID); err != nil {
		t.Fatal(err)
	}
	if err := store.FinishSetup(t.Context(), setupOwner, setup.ID, nil); err != nil {
		t.Fatal(err)
	}
	sessions, err = store.HostSessions(t.Context(), "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	for _, got := range sessions {
		if got.GetSessionId() == setup.ID && (got.GetCentralSamplerV1().GetSetup() != sessionpb.SetupState_SETUP_STATE_INTERRUPTED || got.GetCentralSamplerV1().GetBlocked() != nil) {
			t.Fatalf("setup status=%v", got)
		}
	}
}

func TestHostSessionsUsesRecoveryCausePrecedence(t *testing.T) {
	for _, scenario := range []string{"pending", "intent", "cell", "process", "effects"} {
		t.Run(scenario, func(t *testing.T) {
			store, session, owner := claimedSession(t)
			// Seed all lower-priority blockers too, so the assertion verifies precedence.
			if scenario != "effects" {
				if _, err := store.DB().ExecContext(t.Context(), `INSERT INTO sampler_process(id,conversation_id,generation,status,tty) VALUES('process',$1,$2,'running',false)`, session.ConversationID, owner.Generation); err != nil {
					t.Fatal(err)
				}
			}
			if scenario == "cell" || scenario == "intent" || scenario == "pending" {
				if _, err := store.DB().ExecContext(t.Context(), `INSERT INTO sampler_cell(id,conversation_id,generation,status) VALUES('cell',$1,$2,'live')`, session.ConversationID, owner.Generation); err != nil {
					t.Fatal(err)
				}
			}
			intentID := "00000000-0000-4000-8000-000000000001"
			if scenario == "intent" || scenario == "pending" {
				if _, err := store.DB().ExecContext(t.Context(), `INSERT INTO sampler_intent(id,conversation_id,generation,request) VALUES($1,$2,$3,'{"kind":"tool"}')`, intentID, session.ConversationID, owner.Generation); err != nil {
					t.Fatal(err)
				}
			}
			if scenario == "pending" {
				if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET pending='{"id":"00000000-0000-4000-8000-000000000002","kind":"sampling"}' WHERE id=$1`, session.ConversationID); err != nil {
					t.Fatal(err)
				}
			}
			if err := store.FailExecution(t.Context(), owner, session.ID, nil, errors.New("driver failed at boundary")); err != nil {
				t.Fatal(err)
			}
			sessions, err := store.HostSessions(t.Context(), "fixture-account")
			if err != nil || len(sessions) != 1 {
				t.Fatalf("sessions=%v err=%v", sessions, err)
			}
			blocked := sessions[0].GetCentralSamplerV1().GetBlocked()
			if blocked == nil || !strings.Contains(blocked.GetDetail(), "driver failed at boundary") {
				t.Fatalf("blocked=%v", blocked)
			}
			switch scenario {
			case "pending":
				if got := blocked.GetUnresolvedIntent(); got.GetIntentId() != "00000000-0000-4000-8000-000000000002" || got.GetKind() != "sampling" {
					t.Fatalf("cause=%v", blocked)
				}
			case "intent":
				if got := blocked.GetUnresolvedIntent(); got.GetIntentId() != intentID || got.GetKind() != "tool" {
					t.Fatalf("cause=%v", blocked)
				}
			case "cell":
				if blocked.GetOrphanedCell().GetCellId() != "cell" {
					t.Fatalf("cause=%v", blocked)
				}
			case "process":
				if blocked.GetUnsettledProcess().GetProcessId() != "process" {
					t.Fatalf("cause=%v", blocked)
				}
			case "effects":
				if blocked.GetUnconfirmedEffects() == nil {
					t.Fatalf("cause=%v", blocked)
				}
			}
		})
	}
}
