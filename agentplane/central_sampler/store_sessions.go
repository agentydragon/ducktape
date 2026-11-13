package centralsampler

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/session_instructions"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func (s *Store) CreateSession(ctx context.Context, request *samplerpb.CreateSessionRequest, ownerUID string, api protocol.APIStyle, platformInstructions *string) (*Session, error) {
	if request == nil || request.GetSessionId() == "" || request.GetSpec() == nil || ownerUID == "" || (platformInstructions != nil && strings.TrimSpace(*platformInstructions) == "") {
		return nil, protocol.ErrInvalid
	}
	executor := request.GetExecutor()
	if executor == nil || executor.GetOwner() == nil || executor.GetTarget() == nil || executor.GetOwner().GetNamespace() == "" ||
		executor.GetTarget().GetNamespace() == "" || executor.GetTarget().GetName() == "" || executor.GetTarget().GetUid() == "" ||
		executor.GetTarget().GetNamespace() != executor.GetOwner().GetNamespace() {
		return nil, protocol.ErrInvalid
	}
	placement := request.GetSpec().GetPlacement().GetCentralSamplerV1()
	if placement == nil || placement.GetExecutorEnvironment().GetName() != executor.GetTarget().GetName() ||
		placement.GetExecutorEnvironment().GetUid() != executor.GetTarget().GetUid() {
		return nil, protocol.ErrInvalid
	}
	if principal := request.GetExecutionPrincipal(); principal != nil &&
		(principal.GetAccount().GetNamespace() == "" || principal.GetAccount().GetName() == "" || principal.GetUid() == "" ||
			principal.GetAccount().GetNamespace() != request.GetExecutor().GetOwner().GetNamespace() ||
			principal.GetAccount().GetName() != request.GetExecutor().GetOwner().GetName()) {
		return nil, protocol.ErrInvalid
	}
	if identity := request.GetExecutionIdentity(); identity != nil && (identity.GetName() == "" || identity.GetUid() == "") {
		return nil, protocol.ErrInvalid
	}
	if _, err := newNativeContext(api); err != nil {
		return nil, err
	}
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(request)
	if err != nil {
		return nil, err
	}
	var opened *Session
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, request.SessionId); err != nil {
			return err
		}
		existing, err := scanSession(tx.QueryRowContext(ctx, `SELECT id, conversation_id, account_uid, configuration, model, effort,
			source_id, cursor, active_turn, active_command, workspace_prepared, setup_state, setup_started, blocked, blocked_reason
			FROM sampler_session WHERE id=$1`, request.SessionId))
		if err == nil {
			if existing.AccountUID != ownerUID {
				return ErrForbidden
			}
			previous := &samplerpb.CreateSessionRequest{}
			if err := (proto.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(existing.Configuration, previous); err != nil {
				return err
			}
			incoming := request
			if previous.GetExecutionPrincipal() == nil && request.GetExecutionPrincipal() != nil {
				// A retry cannot assign an owner to a legacy unassociated Session.
				return ErrConflict
			}
			if previous.GetExecutionPrincipal() != nil && request.GetExecutionPrincipal() != nil &&
				!proto.Equal(previous.GetExecutionPrincipal(), request.GetExecutionPrincipal()) {
				return ErrConflict
			}
			if previous.GetExecutionIdentity() == nil && request.GetExecutionIdentity() != nil {
				return ErrConflict
			}
			if previous.GetExecutionPrincipal() != nil && request.GetExecutionPrincipal() == nil ||
				previous.GetExecutionIdentity() != nil && request.GetExecutionIdentity() == nil {
				// Older retrying Dispatchers may omit the additive fields. Compare as if they were present,
				// while leaving the already retained configuration untouched.
				incoming = proto.Clone(request).(*samplerpb.CreateSessionRequest)
				if request.GetExecutionPrincipal() == nil {
					incoming.ExecutionPrincipal = cloneExecutionPrincipal(previous.GetExecutionPrincipal())
				}
				if request.GetExecutionIdentity() == nil {
					incoming.ExecutionIdentity = cloneExecutionIdentity(previous.GetExecutionIdentity())
				}
			}
			if !proto.Equal(previous, incoming) {
				return ErrConflict
			}
			var existingAPI protocol.APIStyle
			if err := tx.QueryRowContext(ctx, `SELECT api FROM sampler_conversation WHERE id=$1`, existing.ConversationID).Scan(&existingAPI); err != nil {
				return err
			}
			if existingAPI != api {
				return protocol.ErrPrecondition
			}
			opened = existing
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		conversationID := uuid.NewString()
		// Freeze the effective prompt at first admission. Session.Configuration below keeps the raw
		// request for idempotency, so a duplicate open never recomposes against changed settings.
		instructions := request.GetSpec().GetInstructions()
		if platformInstructions != nil {
			instructions = sessioninstructions.Compose(*platformInstructions, request.GetSessionId(), instructions)
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_conversation(id,generation,owner,lease_until,pending,stopped,
			executor_session,transcript_control,instructions,context_tokens,api) VALUES($1,0,NULL,NULL,NULL,FALSE,NULL,'{}',$2,NULL,$3)`,
			conversationID, instructions, api); err != nil {
			return err
		}
		setupState := sessionpb.SetupState_SETUP_STATE_NOT_REQUIRED
		if request.GetSetupScript() != "" {
			setupState = sessionpb.SetupState_SETUP_STATE_RUNNING
		}
		opened = &Session{
			ID: request.GetSessionId(), ConversationID: conversationID, AccountUID: ownerUID,
			Configuration: wire, Model: request.GetSpec().GetModel(), Effort: request.GetSpec().GetReasoningEffort(),
			SourceID: uuid.NewString(), SetupState: setupState,
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_session(id,conversation_id,account_uid,configuration,model,effort,
			source_id,cursor,active_turn,active_command,workspace_prepared,setup_state,setup_started,blocked)
			VALUES($1,$2,$3,$4,$5,$6,$7,0,NULL,NULL,FALSE,$8,FALSE,FALSE)`,
			opened.ID, opened.ConversationID, opened.AccountUID, opened.Configuration, opened.Model, opened.Effort, opened.SourceID, int32(setupState)); err != nil {
			return err
		}
		if _, err := appendEvent(ctx, tx, opened, &eventpb.Event{Observation: &eventpb.Event_ExecutionProgress{
			ExecutionProgress: &eventpb.ExecutionProgress{OperationId: opened.ID, Kind: eventpb.ExecutionProgress_KIND_SESSION, Phase: eventpb.ExecutionProgress_PHASE_OPENED},
		}}); err != nil {
			return err
		}
		if setupState != sessionpb.SetupState_SETUP_STATE_RUNNING {
			if _, err := appendEvent(ctx, tx, opened, &eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{}}}); err != nil {
				return err
			}
		} else {
			return notifyWork(ctx, tx)
		}
		return nil
	})
	return opened, err
}

func (s *Store) GetSession(ctx context.Context, id, ownerUID string) (*Session, error) {
	session, err := scanSession(s.db.QueryRowContext(ctx, `SELECT id, conversation_id, account_uid, configuration, model, effort, source_id, cursor,
		active_turn, active_command, workspace_prepared, setup_state, setup_started, blocked, blocked_reason FROM sampler_session WHERE id=$1`, id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, protocol.ErrMissing
	}
	if err != nil {
		return nil, err
	}
	if ownerUID != "" && session.AccountUID != ownerUID {
		return nil, ErrForbidden
	}
	return session, nil
}

// CodeSessionEpoch returns the durable logical JavaScript-session generation for a live owner.
func (s *Store) CodeSessionEpoch(ctx context.Context, owner Owner) (int, error) {
	var epoch int
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		return tx.QueryRowContext(ctx, `SELECT code_session_epoch FROM sampler_conversation WHERE id=$1`, owner.ConversationID).Scan(&epoch)
	})
	return epoch, err
}

// StampCodeHostInstance records a host generation only after a logical session has opened.
// A changed marker must first be reconciled before any cell is dispatched on the new host.
func (s *Store) StampCodeHostInstance(ctx context.Context, owner Owner, instanceID string) error {
	if _, err := uuid.Parse(instanceID); err != nil {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwner(ctx, tx, owner); err != nil {
			return err
		}
		var previous sql.NullString
		if err := tx.QueryRowContext(ctx, `SELECT code_host_instance::text FROM sampler_conversation WHERE id=$1`, owner.ConversationID).Scan(&previous); err != nil {
			return err
		}
		if previous.Valid {
			if previous.String != instanceID {
				return codemode.ErrHostInstanceChanged
			}
			return nil
		}
		_, err := tx.ExecContext(ctx, `UPDATE sampler_conversation SET code_host_instance=$2 WHERE id=$1`, owner.ConversationID, instanceID)
		return err
	})
}

func (s *Store) ListSessions(ctx context.Context, ownerUID string) ([]*Session, error) {
	query := `SELECT id, conversation_id, account_uid, configuration, model, effort, source_id, cursor,
		active_turn, active_command, workspace_prepared, setup_state, setup_started, blocked, blocked_reason FROM sampler_session`
	args := []any{}
	if ownerUID != "" {
		query += ` WHERE account_uid=$1`
		args = append(args, ownerUID)
	}
	query += ` ORDER BY id`
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var sessions []*Session
	for rows.Next() {
		session, err := scanSession(rows)
		if err != nil {
			return nil, err
		}
		sessions = append(sessions, session)
	}
	return sessions, rows.Err()
}

func Describe(session *Session) (*samplerpb.GetSessionResponse, error) {
	if session == nil {
		return nil, protocol.ErrInvalid
	}
	request := &samplerpb.CreateSessionRequest{}
	if err := proto.Unmarshal(session.Configuration, request); err != nil {
		return nil, fmt.Errorf("decode stored session configuration: %w", err)
	}
	spec := proto.Clone(request.GetSpec()).(*sessionpb.SessionSpec)
	spec.Model, spec.ReasoningEffort = session.Model, session.Effort
	state := sessionpb.HarnessState_HARNESS_STATE_RUNNING
	if session.Blocked || session.SetupState == sessionpb.SetupState_SETUP_STATE_RUNNING {
		state = sessionpb.HarnessState_HARNESS_STATE_STOPPED
	}
	outcome := sessionpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED
	if session.SetupState == sessionpb.SetupState_SETUP_STATE_RUNNING {
		outcome = sessionpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING
	} else if session.SetupState == sessionpb.SetupState_SETUP_STATE_FAILED || session.SetupState == sessionpb.SetupState_SETUP_STATE_INTERRUPTED {
		outcome = sessionpb.LaunchOutcome_LAUNCH_OUTCOME_FAILED
	}
	attached := &sessionpb.Attached{
		SessionId: session.ID, Spec: spec, LastCursor: session.Cursor,
		HarnessState: state, SetupState: session.SetupState, LaunchOutcome: outcome,
		ExecutionPrincipal: cloneExecutionPrincipal(request.GetExecutionPrincipal()),
		ExecutionIdentity:  cloneExecutionIdentity(request.GetExecutionIdentity()),
	}
	if session.ActiveTurn != nil {
		attached.ActiveTurnId = *session.ActiveTurn
	}
	var incarnationUID *string
	if value := request.GetExpectedIncarnationUid(); value != "" {
		incarnationUID = &value
	}
	attached.IncarnationUid = incarnationUID
	blockedReason := ""
	if session.BlockedReason != nil {
		blockedReason = *session.BlockedReason
	}
	return &samplerpb.GetSessionResponse{
		Attached: attached, Executor: request.GetExecutor(),
		ExpectedIncarnationUid: request.GetExpectedIncarnationUid(), Blocked: session.Blocked,
		BlockedReason: blockedReason,
	}, nil
}

func cloneExecutionPrincipal(identity *identitypb.ServiceAccountIdentity) *identitypb.ServiceAccountIdentity {
	if identity == nil {
		return nil
	}
	return proto.Clone(identity).(*identitypb.ServiceAccountIdentity)
}

func cloneExecutionIdentity(identity *identitypb.Identity) *identitypb.Identity {
	if identity == nil {
		return nil
	}
	return proto.Clone(identity).(*identitypb.Identity)
}
