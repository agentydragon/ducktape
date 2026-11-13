package centralsampler

import (
	"context"
	"database/sql"
	"fmt"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/protobuf/proto"
)

// SetExecutorSession records the exec-server session and the shell it reported, if any.
func (s *Store) SetExecutorSession(ctx context.Context, owner Owner, sessionID string, shell *string) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.ExecutorSession != nil && *conversation.ExecutorSession != sessionID {
			liveProcess, err := hasLiveProcess(ctx, tx, conversation.ID)
			if err != nil {
				return err
			}
			// A JS cell can make its first executor call after setup detached. Only
			// remote process handles pin the retained executor session; the cell's
			// independent host lifetime does not establish any executor dependency.
			if liveProcess {
				return &protocol.UnknownEffectError{Reason: "executor returned another session; existing handles cannot be replaced"}
			}
		}
		conversation.ExecutorSession = &sessionID
		conversation.ExecutorShell = shell
		return updateConversation(ctx, tx, conversation)
	})
}

func (s *Store) environmentContext(conversation *Conversation, session *Session) (EnvironmentContext, error) {
	request := &samplerpb.CreateSessionRequest{}
	if err := proto.Unmarshal(session.Configuration, request); err != nil {
		return EnvironmentContext{}, err
	}
	return currentEnvironmentContext(request.GetSpec().GetCwd(), conversation.ExecutorShell, s.now()), nil
}

// recordEnvironmentContext appends the update from the conversation's baseline to the Session's
// current environment, then advances the baseline. The caller persists the conversation.
func (s *Store) recordEnvironmentContext(conversation *Conversation, session *Session) error {
	current, err := s.environmentContext(conversation, session)
	if err != nil {
		return err
	}
	if update := environmentContextUpdate(conversation.EnvironmentContext, current); update != "" {
		if err := conversation.apply(func(state protocol.Context) error {
			return state.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: update}})
		}); err != nil {
			return err
		}
	}
	conversation.EnvironmentContext = &current
	return nil
}

func persistSession(ctx context.Context, tx *sql.Tx, session *Session) error {
	_, err := tx.ExecContext(ctx, `UPDATE sampler_session SET model=$2,effort=$3,active_turn=$4,active_command=$5,
		workspace_prepared=$6,setup_state=$7,setup_started=$8,blocked=$9,blocked_reason=$10,cursor=$11 WHERE id=$1`,
		session.ID, session.Model, session.Effort, nullableString(session.ActiveTurn), nullableString(session.ActiveCommand),
		session.WorkspacePrepared, int32(session.SetupState), session.SetupStarted, session.Blocked, nullableString(session.BlockedReason), session.Cursor)
	return err
}

func (s *Store) PreparedWorkspace(ctx context.Context, owner Owner, sessionID string) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		session.WorkspacePrepared = true
		if err := appendProgress(ctx, tx, session, &eventpb.ExecutionProgress{OperationId: sessionID, Kind: eventpb.ExecutionProgress_KIND_WORKSPACE, Phase: eventpb.ExecutionProgress_PHASE_PREPARED}); err != nil {
			return err
		}
		return persistSession(ctx, tx, session)
	})
}

func (s *Store) StartSetup(ctx context.Context, owner Owner, sessionID string) (bool, error) {
	started := false
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		if session.SetupStarted {
			return s.settleSetup(ctx, tx, conversation, session, nil)
		}
		session.SetupStarted = true
		if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_SetupStarted{SetupStarted: &eventpb.SetupStarted{}}}); err != nil {
			return err
		}
		started = true
		return persistSession(ctx, tx, session)
	})
	return started, err
}

func (s *Store) SetupOutput(ctx context.Context, owner Owner, sessionID, stream string, data []byte) error {
	if stream != "stdout" && stream != "stderr" {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		output := &eventpb.SetupOutput{}
		if stream == "stdout" {
			output.Stream = &eventpb.SetupOutput_Stdout{Stdout: append([]byte(nil), data...)}
		} else {
			output.Stream = &eventpb.SetupOutput_Stderr{Stderr: append([]byte(nil), data...)}
		}
		_, err = appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_SetupOutput{SetupOutput: output}})
		if err != nil {
			return err
		}
		return persistSession(ctx, tx, session)
	})
}

func (s *Store) FinishSetup(ctx context.Context, owner Owner, sessionID string, exitCode *int) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		return s.settleSetup(ctx, tx, conversation, session, exitCode)
	})
}

func (s *Store) settleSetup(ctx context.Context, tx *sql.Tx, conversation *Conversation, session *Session, exitCode *int) error {
	if session.SetupState != sessionpb.SetupState_SETUP_STATE_RUNNING {
		return protocol.ErrPrecondition
	}
	session.Blocked = false
	session.BlockedReason = nil
	if exitCode == nil {
		session.SetupState = sessionpb.SetupState_SETUP_STATE_INTERRUPTED
		if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_SetupInterrupted{SetupInterrupted: &eventpb.SetupInterrupted{}}}); err != nil {
			return err
		}
	} else {
		if *exitCode == 0 {
			session.SetupState = sessionpb.SetupState_SETUP_STATE_SUCCEEDED
		} else {
			session.SetupState = sessionpb.SetupState_SETUP_STATE_FAILED
		}
		if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_SetupFinished{
			SetupFinished: &eventpb.SetupFinished{ExitCode: int32(*exitCode)},
		}}); err != nil {
			return err
		}
	}
	if exitCode != nil && *exitCode == 0 {
		if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{}}}); err != nil {
			return err
		}
	} else {
		session.Blocked = true
		var reason string
		if exitCode == nil {
			reason = "setup was interrupted; inspect retained setup output and executor state before opening a new Session because setup attempts are never replayed"
		} else {
			reason = fmt.Sprintf("setup exited with status %d; inspect retained setup output, correct the setup, and open a new Session because setup attempts are never replayed", *exitCode)
		}
		session.BlockedReason = &reason
		conversation.Stopped = true
		if err := s.blockQueue(ctx, tx, session, reason); err != nil {
			return err
		}
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
	}
	return persistSession(ctx, tx, session)
}

func appendProgress(ctx context.Context, tx *sql.Tx, session *Session, progress *eventpb.ExecutionProgress) error {
	_, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_ExecutionProgress{ExecutionProgress: progress}})
	return err
}
