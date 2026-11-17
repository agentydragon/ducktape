package storagecodec

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// The column order is also the order of EntryContentStorage.Args and Destinations.
const EntryContentReadColumns = `action_state, action_at,
	action_actor_kind, action_actor_namespace, action_actor_name, action_actor_issuer, action_actor_subject, action_actor_identity_name, action_actor_identity_uid,
	mergeability_repository, mergeability_base_ref, mergeability_head_sha, mergeability_base_sha,
	mergeability_previous, mergeability_current, mergeability_observed_at, mergeability_observed_at_submicro_nanos,
	merge_commit_sha, merge_commit_base_sha, merge_commit_head_sha, message_body, message_from_session`

// EntryContentStorage is the neutral row shape for native Action, GitHub and Message content columns.
type EntryContentStorage struct {
	ActionState                         *int32
	ActionAt                            *time.Time
	ActionActorKind                     *string
	ActionActorNamespace                *string
	ActionActorName                     *string
	ActionActorIssuer                   *string
	ActionActorSubject                  *string
	ActionActorIdentityName             *string
	ActionActorIdentityUID              *string
	MergeabilityRepository              *string
	MergeabilityBaseRef                 *string
	MergeabilityHeadSHA                 *string
	MergeabilityBaseSHA                 *string
	MergeabilityPrevious                *string
	MergeabilityCurrent                 *string
	MergeabilityObservedAt              *time.Time
	MergeabilityObservedAtSubmicroNanos *int16
	MergeCommitSHA                      *string
	MergeCommitBaseSHA                  *string
	MergeCommitHeadSHA                  *string
	MessageBody                         *string
	MessageFromSession                  *string
}

func (c EntryContentStorage) Args() []any {
	return []any{
		c.ActionState, c.ActionAt,
		c.ActionActorKind, c.ActionActorNamespace, c.ActionActorName, c.ActionActorIssuer, c.ActionActorSubject, c.ActionActorIdentityName, c.ActionActorIdentityUID,
		c.MergeabilityRepository, c.MergeabilityBaseRef, c.MergeabilityHeadSHA, c.MergeabilityBaseSHA,
		c.MergeabilityPrevious, c.MergeabilityCurrent, c.MergeabilityObservedAt, c.MergeabilityObservedAtSubmicroNanos,
		c.MergeCommitSHA, c.MergeCommitBaseSHA, c.MergeCommitHeadSHA, c.MessageBody, c.MessageFromSession,
	}
}

func (c *EntryContentStorage) Destinations() []any {
	return []any{
		&c.ActionState, &c.ActionAt,
		&c.ActionActorKind, &c.ActionActorNamespace, &c.ActionActorName, &c.ActionActorIssuer, &c.ActionActorSubject, &c.ActionActorIdentityName, &c.ActionActorIdentityUID,
		&c.MergeabilityRepository, &c.MergeabilityBaseRef, &c.MergeabilityHeadSHA, &c.MergeabilityBaseSHA,
		&c.MergeabilityPrevious, &c.MergeabilityCurrent, &c.MergeabilityObservedAt, &c.MergeabilityObservedAtSubmicroNanos,
		&c.MergeCommitSHA, &c.MergeCommitBaseSHA, &c.MergeCommitHeadSHA, &c.MessageBody, &c.MessageFromSession,
	}
}

// ActionEntryContentStorageFor derives native Action content from its canonical JSON payload.
func ActionEntryContentStorageFor(event model.ActionsEvent, payload json.RawMessage) (EntryContentStorage, error) {
	var c EntryContentStorage
	var action ActionEvent
	if err := json.Unmarshal(payload, &action); err != nil {
		return c, fmt.Errorf("decode Action content: %w", err)
	}
	if action.Sequence != event.Sequence {
		return c, fmt.Errorf("Action content sequence %d does not match identity sequence %d", action.Sequence, event.Sequence)
	}
	if err := timestamppb.New(action.At).CheckValid(); err != nil {
		return c, fmt.Errorf("Action content at: %w", err)
	}
	number := int32(action.State)
	c.ActionState = &number
	c.ActionAt = &action.At
	if action.Actor != nil {
		switch principal := action.Actor.GetPrincipal().(type) {
		case *action_servicepb.Principal_Caller:
			kind := "caller"
			namespace, name := principal.Caller.GetNamespace(), principal.Caller.GetName()
			c.ActionActorKind, c.ActionActorNamespace, c.ActionActorName = &kind, &namespace, &name
		case *action_servicepb.Principal_CallerIdentity:
			kind := "identity"
			name, uid := principal.CallerIdentity.GetName(), principal.CallerIdentity.GetUid()
			c.ActionActorKind, c.ActionActorIdentityName, c.ActionActorIdentityUID = &kind, &name, &uid
		case *action_servicepb.Principal_Operator:
			kind := "operator"
			issuer, subject := principal.Operator.GetIssuer(), principal.Operator.GetSubject()
			c.ActionActorKind, c.ActionActorIssuer, c.ActionActorSubject = &kind, &issuer, &subject
		default:
			return c, errors.New("unknown Action actor variant")
		}
	}
	return c, nil
}

// MessageEntryContentStorageFor derives native Message content from its canonical JSON payload.
func MessageEntryContentStorageFor(payload json.RawMessage) (EntryContentStorage, error) {
	var c EntryContentStorage
	var wire struct {
		Body        *string `json:"body"`
		FromSession *string `json:"from_session"`
	}
	if err := Decode(payload, &wire); err != nil {
		return c, fmt.Errorf("decode message content: %w", err)
	}
	if wire.Body == nil {
		return c, errors.New("message content has no body")
	}
	c.MessageBody, c.MessageFromSession = wire.Body, wire.FromSession
	return c, nil
}

// ActionPayload reconstructs the public Action payload from its native storage columns.
func (c EntryContentStorage) ActionPayload(event model.ActionsEvent) (json.RawMessage, error) {
	if c.ActionState == nil {
		return nil, errors.New("entry has no Action content")
	}
	if c.ActionAt == nil {
		return nil, errors.New("entry has no Action time")
	}
	var actor *action_servicepb.Principal
	if c.ActionActorKind != nil {
		switch *c.ActionActorKind {
		case "caller":
			if c.ActionActorNamespace == nil || c.ActionActorName == nil {
				return nil, errors.New("incomplete caller actor")
			}
			actor = &action_servicepb.Principal{Principal: &action_servicepb.Principal_Caller{Caller: ServiceAccountProto(model.ServiceAccount{Namespace: *c.ActionActorNamespace, Name: *c.ActionActorName})}}
		case "identity":
			if c.ActionActorIdentityName == nil || c.ActionActorIdentityUID == nil {
				return nil, errors.New("incomplete Identity actor")
			}
			actor = &action_servicepb.Principal{Principal: &action_servicepb.Principal_CallerIdentity{CallerIdentity: &identitypb.Identity{Name: *c.ActionActorIdentityName, Uid: *c.ActionActorIdentityUID}}}
		case "operator":
			if c.ActionActorIssuer == nil || c.ActionActorSubject == nil {
				return nil, errors.New("incomplete operator actor")
			}
			actor = &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{Issuer: *c.ActionActorIssuer, Subject: *c.ActionActorSubject}}}
		default:
			return nil, fmt.Errorf("unknown Action actor kind %q", *c.ActionActorKind)
		}
	}
	return json.Marshal(ActionEvent{Sequence: event.Sequence, State: action_servicepb.ActionState(*c.ActionState), At: *c.ActionAt, Actor: actor})
}

// MessagePayload reconstructs the public Message payload from its native storage columns.
func (c EntryContentStorage) MessagePayload() (json.RawMessage, error) {
	if c.MessageBody == nil {
		return nil, errors.New("entry has no Message content")
	}
	return json.Marshal(struct {
		Body        string  `json:"body"`
		FromSession *string `json:"from_session,omitempty"`
	}{*c.MessageBody, c.MessageFromSession})
}
