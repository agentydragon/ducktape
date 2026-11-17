package sessionstore

import (
	"context"
	"database/sql"
	"encoding/json"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

// HostSessions reads the caller's current Session status from one database snapshot.
// It does not take ownership leases or infer executor/Sandbox liveness. The active
// command has already been applied to the loop even while its run outcome remains accepted.
func (s *Store) HostSessions(ctx context.Context, ownerKey string) ([]*harnesshostpb.Session, error) {
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(ctx, `SELECT s.id,s.conversation_id,s.active_turn,s.setup_state,s.blocked,s.blocked_reason,c.pending,
  (SELECT count(*) FROM sampler_command q WHERE q.session_id=s.id AND q.outcome=$2 AND q.id IS DISTINCT FROM s.active_command)
  FROM sampler_session s JOIN sampler_conversation c ON c.id=s.conversation_id
  WHERE COALESCE(s.service_spiffe_id,s.account_uid)=$1 ORDER BY s.id`, ownerKey, int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
	if err != nil {
		return nil, err
	}
	type record struct {
		session      *harnesshostpb.Session
		conversation Conversation
		blocked      bool
		detail       sql.NullString
		pending      []byte
	}
	var records []record
	for rows.Next() {
		host := &harnesshostpb.CentralSamplerSession{}
		r := record{session: &harnesshostpb.Session{Host: &harnesshostpb.Session_CentralSamplerV1{CentralSamplerV1: host}}}
		var active sql.NullString
		var setup int32
		if err := rows.Scan(&r.session.SessionId, &r.conversation.ID, &active, &setup, &r.blocked, &r.detail, &r.pending, &r.session.PendingCommands); err != nil {
			rows.Close()
			return nil, err
		}
		r.session.ActiveTurnId = active.String
		host.Setup = sessionpb.SetupState(setup)
		records = append(records, r)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()
	result := make([]*harnesshostpb.Session, 0, len(records))
	for _, r := range records {
		host := r.session.GetCentralSamplerV1()
		// Failed/interrupted setup already explains this permanent refusal. It is not
		// an unconfirmed execution effect and has no Blocked cause in the contract.
		if r.blocked && host.Setup != sessionpb.SetupState_SETUP_STATE_FAILED && host.Setup != sessionpb.SetupState_SETUP_STATE_INTERRUPTED {
			if len(r.pending) != 0 && string(r.pending) != "null" {
				if err := json.Unmarshal(r.pending, &r.conversation.Pending); err != nil {
					return nil, err
				}
			}
			cause, err := findBlockingCause(ctx, tx, &r.conversation)
			if err != nil {
				return nil, err
			}
			host.Blocked = cause.hostBlocked(r.detail.String)
		}
		result = append(result, r.session)
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return result, nil
}

func (c blockingCause) hostBlocked(detail string) *harnesshostpb.Blocked {
	result := &harnesshostpb.Blocked{Detail: detail}
	switch c.kind {
	case "intent":
		result.Cause = &harnesshostpb.Blocked_UnresolvedIntent{UnresolvedIntent: &harnesshostpb.UnresolvedIntent{IntentId: c.id, Kind: c.intentKind}}
	case "cell":
		result.Cause = &harnesshostpb.Blocked_OrphanedCell{OrphanedCell: &harnesshostpb.OrphanedCell{CellId: c.id}}
	case "process":
		result.Cause = &harnesshostpb.Blocked_UnsettledProcess{UnsettledProcess: &harnesshostpb.UnsettledProcess{ProcessId: c.id}}
	default:
		result.Cause = &harnesshostpb.Blocked_UnconfirmedEffects{UnconfirmedEffects: &harnesshostpb.UnconfirmedEffects{}}
	}
	return result
}
