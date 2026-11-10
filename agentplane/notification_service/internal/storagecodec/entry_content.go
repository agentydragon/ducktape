package storagecodec

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// The column order is also the order of EntryContentStorage.args and destinations.
const EntryContentReadColumns = `action_state, action_at,
	action_actor_kind, action_actor_namespace, action_actor_name, action_actor_issuer, action_actor_subject,
	mergeability_repository, mergeability_base_ref, mergeability_head_sha, mergeability_base_sha,
	mergeability_previous, mergeability_current, mergeability_observed_at, mergeability_observed_at_submicro_nanos,
	merge_commit_sha, merge_commit_base_sha, merge_commit_head_sha, message_body, message_from_session`

type EntryContentStorage struct {
	ActionState                         *int32
	ActionAt                            *time.Time
	ActionActorKind                     *string
	ActionActorNamespace                *string
	ActionActorName                     *string
	ActionActorIssuer                   *string
	ActionActorSubject                  *string
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
		c.ActionActorKind, c.ActionActorNamespace, c.ActionActorName, c.ActionActorIssuer, c.ActionActorSubject,
		c.MergeabilityRepository, c.MergeabilityBaseRef, c.MergeabilityHeadSHA, c.MergeabilityBaseSHA,
		c.MergeabilityPrevious, c.MergeabilityCurrent, c.MergeabilityObservedAt, c.MergeabilityObservedAtSubmicroNanos,
		c.MergeCommitSHA, c.MergeCommitBaseSHA, c.MergeCommitHeadSHA, c.MessageBody, c.MessageFromSession,
	}
}

func (c *EntryContentStorage) Destinations() []any {
	return []any{
		&c.ActionState, &c.ActionAt,
		&c.ActionActorKind, &c.ActionActorNamespace, &c.ActionActorName, &c.ActionActorIssuer, &c.ActionActorSubject,
		&c.MergeabilityRepository, &c.MergeabilityBaseRef, &c.MergeabilityHeadSHA, &c.MergeabilityBaseSHA,
		&c.MergeabilityPrevious, &c.MergeabilityCurrent, &c.MergeabilityObservedAt, &c.MergeabilityObservedAtSubmicroNanos,
		&c.MergeCommitSHA, &c.MergeCommitBaseSHA, &c.MergeCommitHeadSHA, &c.MessageBody, &c.MessageFromSession,
	}
}

func contentTimestamp(t time.Time) (*time.Time, *int16) {
	truncated := t.Truncate(time.Microsecond)
	residual := int16(t.Sub(truncated) / time.Nanosecond)
	return &truncated, &residual
}

func restoreContentTimestamp(t *time.Time, residual *int16) (time.Time, error) {
	if t == nil || residual == nil || *residual < 0 || *residual > 999 {
		return time.Time{}, errors.New("incomplete content timestamp")
	}
	return t.Add(time.Duration(*residual) * time.Nanosecond), nil
}

// EntryContentStorageFor derives only non-key content. Source identities live in the existing entry columns.
// GitHub webhook and observation JSON stays in payload and has no native content columns.
func EntryContentStorageFor(event model.EventIdentity, payload json.RawMessage) (EntryContentStorage, error) {
	var c EntryContentStorage
	switch event := event.(type) {
	case model.ActionsEvent:
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
		actor := action.Actor
		if actor != nil {
			switch principal := actor.GetPrincipal().(type) {
			case *action_servicepb.Principal_Caller:
				kind := "caller"
				namespace, name := principal.Caller.GetNamespace(), principal.Caller.GetName()
				c.ActionActorKind, c.ActionActorNamespace, c.ActionActorName = &kind, &namespace, &name
			case *action_servicepb.Principal_Operator:
				kind := "operator"
				issuer, subject := principal.Operator.GetIssuer(), principal.Operator.GetSubject()
				c.ActionActorKind, c.ActionActorIssuer, c.ActionActorSubject = &kind, &issuer, &subject
			default:
				return c, errors.New("unknown Action actor variant")
			}
		}
	case model.GitHubEvent, model.GitHubObservationEvent:
		return c, nil
	case model.GitHubMergeabilityEvent:
		return mergeabilityContentFromJSON(payload, event.PullRequestNumber)
	case model.GitHubMergeabilitySnapshotEvent:
		return mergeabilityContentFromJSON(payload, event.PullRequestNumber)
	case model.MessageEvent:
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
	default:
		return c, fmt.Errorf("unsupported event identity type %T", event)
	}
	return c, nil
}

func mergeabilityContentFromJSON(payload json.RawMessage, identityNumber int64) (EntryContentStorage, error) {
	var c EntryContentStorage
	var wire struct {
		Repository           *string                  `json:"repository"`
		PullRequestNumber    *int64                   `json:"pullRequestNumber"`
		BaseRef              *string                  `json:"baseRef"`
		HeadSHA              *string                  `json:"headSha"`
		BaseSHA              *string                  `json:"baseSha"`
		PreviousMergeability *string                  `json:"previousMergeability"`
		Mergeability         *string                  `json:"mergeability"`
		ObservedAt           *time.Time               `json:"observedAt"`
		MergeCommit          *model.GitHubMergeCommit `json:"mergeCommit"`
	}
	if err := Decode(payload, &wire); err != nil {
		return c, fmt.Errorf("decode mergeability content: %w", err)
	}
	if wire.Repository == nil || wire.PullRequestNumber == nil || wire.BaseRef == nil || wire.HeadSHA == nil ||
		wire.BaseSHA == nil || wire.PreviousMergeability == nil || wire.Mergeability == nil || wire.ObservedAt == nil {
		return c, errors.New("mergeability content is incomplete")
	}
	if *wire.PullRequestNumber != identityNumber {
		return c, fmt.Errorf("mergeability content pull request %d does not match identity number %d", *wire.PullRequestNumber, identityNumber)
	}
	for _, state := range []string{*wire.PreviousMergeability, *wire.Mergeability} {
		switch model.GitHubMergeableState(state) {
		case model.GitHubMergeabilityUnknown, model.GitHubMergeable, model.GitHubConflicting:
		default:
			return c, fmt.Errorf("unknown mergeability state %q", state)
		}
	}
	if err := timestamppb.New(*wire.ObservedAt).CheckValid(); err != nil {
		return c, fmt.Errorf("mergeability observedAt: %w", err)
	}
	c.MergeabilityRepository, c.MergeabilityBaseRef = wire.Repository, wire.BaseRef
	c.MergeabilityHeadSHA, c.MergeabilityBaseSHA = wire.HeadSHA, wire.BaseSHA
	c.MergeabilityPrevious, c.MergeabilityCurrent = wire.PreviousMergeability, wire.Mergeability
	c.MergeabilityObservedAt, c.MergeabilityObservedAtSubmicroNanos = contentTimestamp(*wire.ObservedAt)
	if wire.MergeCommit != nil {
		c.MergeCommitSHA = &wire.MergeCommit.SHA
		c.MergeCommitBaseSHA = &wire.MergeCommit.BaseSHA
		c.MergeCommitHeadSHA = &wire.MergeCommit.HeadSHA
	}
	return c, nil
}

func (c EntryContentStorage) Payload(event model.EventIdentity) (json.RawMessage, error) {
	switch event := event.(type) {
	case model.ActionsEvent:
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
	case model.GitHubMergeabilityEvent:
		return c.mergeabilityPayload(event.PullRequestNumber)
	case model.GitHubMergeabilitySnapshotEvent:
		return c.mergeabilityPayload(event.PullRequestNumber)
	case model.MessageEvent:
		if c.MessageBody == nil {
			return nil, errors.New("entry has no Message content")
		}
		return json.Marshal(struct {
			Body        string  `json:"body"`
			FromSession *string `json:"from_session,omitempty"`
		}{*c.MessageBody, c.MessageFromSession})
	default:
		return nil, fmt.Errorf("provider %T has no native content", event)
	}
}

func (c EntryContentStorage) mergeabilityPayload(number int64) (json.RawMessage, error) {
	if c.MergeabilityRepository == nil || c.MergeabilityBaseRef == nil || c.MergeabilityHeadSHA == nil ||
		c.MergeabilityBaseSHA == nil || c.MergeabilityPrevious == nil || c.MergeabilityCurrent == nil {
		return nil, errors.New("entry has incomplete mergeability content")
	}
	at, err := restoreContentTimestamp(c.MergeabilityObservedAt, c.MergeabilityObservedAtSubmicroNanos)
	if err != nil {
		return nil, err
	}
	var commit *model.GitHubMergeCommit
	if c.MergeCommitSHA != nil {
		if c.MergeCommitBaseSHA == nil || c.MergeCommitHeadSHA == nil {
			return nil, errors.New("entry has incomplete merge commit")
		}
		commit = &model.GitHubMergeCommit{SHA: *c.MergeCommitSHA, BaseSHA: *c.MergeCommitBaseSHA, HeadSHA: *c.MergeCommitHeadSHA}
	}
	return json.Marshal(MergeabilityPayload{
		Repository: *c.MergeabilityRepository, PullRequestNumber: number, BaseRef: *c.MergeabilityBaseRef,
		HeadSHA: *c.MergeabilityHeadSHA, BaseSHA: *c.MergeabilityBaseSHA,
		PreviousMergeability: model.GitHubMergeableState(*c.MergeabilityPrevious),
		Mergeability:         model.GitHubMergeableState(*c.MergeabilityCurrent), ObservedAt: at, MergeCommit: commit,
	})
}

// EntryPayloadFromRead reconstructs the public payload from the provider's canonical storage.
// Webhook and observation payloads are native JSONB; owned sources use native columns.
func EntryPayloadFromRead(event model.EventIdentity, githubPayload []byte, content EntryContentStorage) (json.RawMessage, error) {
	switch event.(type) {
	case model.GitHubEvent, model.GitHubObservationEvent:
		if githubPayload == nil {
			return nil, errors.New("GitHub payload expired before the inbox's expired prefix")
		}
		return json.RawMessage(githubPayload), nil
	default:
		return content.Payload(event)
	}
}
