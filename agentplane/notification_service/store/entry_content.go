package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// The column order is also the order of entryContentStorage.args and destinations.
const entryContentReadColumns = `action_state, action_at,
	action_actor_kind, action_actor_namespace, action_actor_name, action_actor_issuer, action_actor_subject,
	mergeability_repository, mergeability_base_ref, mergeability_head_sha, mergeability_base_sha,
	mergeability_previous, mergeability_current, mergeability_observed_at, mergeability_observed_at_submicro_nanos,
	merge_commit_sha, merge_commit_base_sha, merge_commit_head_sha, message_body, message_from_session`

type entryContentStorage struct {
	actionState                         *int32
	actionAt                            *time.Time
	actionActorKind                     *string
	actionActorNamespace                *string
	actionActorName                     *string
	actionActorIssuer                   *string
	actionActorSubject                  *string
	mergeabilityRepository              *string
	mergeabilityBaseRef                 *string
	mergeabilityHeadSHA                 *string
	mergeabilityBaseSHA                 *string
	mergeabilityPrevious                *string
	mergeabilityCurrent                 *string
	mergeabilityObservedAt              *time.Time
	mergeabilityObservedAtSubmicroNanos *int16
	mergeCommitSHA                      *string
	mergeCommitBaseSHA                  *string
	mergeCommitHeadSHA                  *string
	messageBody                         *string
	messageFromSession                  *string
}

func (c entryContentStorage) args() []any {
	return []any{c.actionState, c.actionAt,
		c.actionActorKind, c.actionActorNamespace, c.actionActorName, c.actionActorIssuer, c.actionActorSubject,
		c.mergeabilityRepository, c.mergeabilityBaseRef, c.mergeabilityHeadSHA, c.mergeabilityBaseSHA,
		c.mergeabilityPrevious, c.mergeabilityCurrent, c.mergeabilityObservedAt, c.mergeabilityObservedAtSubmicroNanos,
		c.mergeCommitSHA, c.mergeCommitBaseSHA, c.mergeCommitHeadSHA, c.messageBody, c.messageFromSession}
}

func (c *entryContentStorage) destinations() []any {
	return []any{&c.actionState, &c.actionAt,
		&c.actionActorKind, &c.actionActorNamespace, &c.actionActorName, &c.actionActorIssuer, &c.actionActorSubject,
		&c.mergeabilityRepository, &c.mergeabilityBaseRef, &c.mergeabilityHeadSHA, &c.mergeabilityBaseSHA,
		&c.mergeabilityPrevious, &c.mergeabilityCurrent, &c.mergeabilityObservedAt, &c.mergeabilityObservedAtSubmicroNanos,
		&c.mergeCommitSHA, &c.mergeCommitBaseSHA, &c.mergeCommitHeadSHA, &c.messageBody, &c.messageFromSession}
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

// entryContentStorageFor derives only non-key content. Source identities live in the existing entry columns.
// GitHub webhook and observation JSON stays in payload and has no native content columns.
func entryContentStorageFor(event model.EventIdentity, payload json.RawMessage) (entryContentStorage, error) {
	var c entryContentStorage
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
		c.actionState = &number
		c.actionAt = &action.At
		actor := action.Actor
		if actor != nil {
			switch principal := actor.GetPrincipal().(type) {
			case *action_servicepb.Principal_Caller:
				kind := "caller"
				namespace, name := principal.Caller.GetNamespace(), principal.Caller.GetName()
				c.actionActorKind, c.actionActorNamespace, c.actionActorName = &kind, &namespace, &name
			case *action_servicepb.Principal_Operator:
				kind := "operator"
				issuer, subject := principal.Operator.GetIssuer(), principal.Operator.GetSubject()
				c.actionActorKind, c.actionActorIssuer, c.actionActorSubject = &kind, &issuer, &subject
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
		if err := decode(payload, &wire); err != nil {
			return c, fmt.Errorf("decode message content: %w", err)
		}
		if wire.Body == nil {
			return c, errors.New("message content has no body")
		}
		c.messageBody, c.messageFromSession = wire.Body, wire.FromSession
	default:
		return c, fmt.Errorf("unsupported event identity type %T", event)
	}
	return c, nil
}

func mergeabilityContentFromJSON(payload json.RawMessage, identityNumber int64) (entryContentStorage, error) {
	var c entryContentStorage
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
	if err := decode(payload, &wire); err != nil {
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
	c.mergeabilityRepository, c.mergeabilityBaseRef = wire.Repository, wire.BaseRef
	c.mergeabilityHeadSHA, c.mergeabilityBaseSHA = wire.HeadSHA, wire.BaseSHA
	c.mergeabilityPrevious, c.mergeabilityCurrent = wire.PreviousMergeability, wire.Mergeability
	c.mergeabilityObservedAt, c.mergeabilityObservedAtSubmicroNanos = contentTimestamp(*wire.ObservedAt)
	if wire.MergeCommit != nil {
		c.mergeCommitSHA = &wire.MergeCommit.SHA
		c.mergeCommitBaseSHA = &wire.MergeCommit.BaseSHA
		c.mergeCommitHeadSHA = &wire.MergeCommit.HeadSHA
	}
	return c, nil
}

func (c entryContentStorage) payload(event model.EventIdentity) (json.RawMessage, error) {
	switch event := event.(type) {
	case model.ActionsEvent:
		if c.actionState == nil {
			return nil, errors.New("entry has no Action content")
		}
		if c.actionAt == nil {
			return nil, errors.New("entry has no Action time")
		}
		var actor *action_servicepb.Principal
		if c.actionActorKind != nil {
			switch *c.actionActorKind {
			case "caller":
				if c.actionActorNamespace == nil || c.actionActorName == nil {
					return nil, errors.New("incomplete caller actor")
				}
				actor = &action_servicepb.Principal{Principal: &action_servicepb.Principal_Caller{Caller: serviceAccountProto(model.ServiceAccount{Namespace: *c.actionActorNamespace, Name: *c.actionActorName})}}
			case "operator":
				if c.actionActorIssuer == nil || c.actionActorSubject == nil {
					return nil, errors.New("incomplete operator actor")
				}
				actor = &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{Issuer: *c.actionActorIssuer, Subject: *c.actionActorSubject}}}
			default:
				return nil, fmt.Errorf("unknown Action actor kind %q", *c.actionActorKind)
			}
		}
		return json.Marshal(ActionEvent{Sequence: event.Sequence, State: action_servicepb.ActionState(*c.actionState), At: *c.actionAt, Actor: actor})
	case model.GitHubMergeabilityEvent:
		return c.mergeabilityPayload(event.PullRequestNumber)
	case model.GitHubMergeabilitySnapshotEvent:
		return c.mergeabilityPayload(event.PullRequestNumber)
	case model.MessageEvent:
		if c.messageBody == nil {
			return nil, errors.New("entry has no Message content")
		}
		return json.Marshal(struct {
			Body        string  `json:"body"`
			FromSession *string `json:"from_session,omitempty"`
		}{*c.messageBody, c.messageFromSession})
	default:
		return nil, fmt.Errorf("provider %T has no native content", event)
	}
}

func (c entryContentStorage) mergeabilityPayload(number int64) (json.RawMessage, error) {
	if c.mergeabilityRepository == nil || c.mergeabilityBaseRef == nil || c.mergeabilityHeadSHA == nil ||
		c.mergeabilityBaseSHA == nil || c.mergeabilityPrevious == nil || c.mergeabilityCurrent == nil {
		return nil, errors.New("entry has incomplete mergeability content")
	}
	at, err := restoreContentTimestamp(c.mergeabilityObservedAt, c.mergeabilityObservedAtSubmicroNanos)
	if err != nil {
		return nil, err
	}
	var commit *model.GitHubMergeCommit
	if c.mergeCommitSHA != nil {
		if c.mergeCommitBaseSHA == nil || c.mergeCommitHeadSHA == nil {
			return nil, errors.New("entry has incomplete merge commit")
		}
		commit = &model.GitHubMergeCommit{SHA: *c.mergeCommitSHA, BaseSHA: *c.mergeCommitBaseSHA, HeadSHA: *c.mergeCommitHeadSHA}
	}
	return json.Marshal(mergeabilityPayload{
		Repository: *c.mergeabilityRepository, PullRequestNumber: number, BaseRef: *c.mergeabilityBaseRef,
		HeadSHA: *c.mergeabilityHeadSHA, BaseSHA: *c.mergeabilityBaseSHA,
		PreviousMergeability: model.GitHubMergeableState(*c.mergeabilityPrevious),
		Mergeability:         model.GitHubMergeableState(*c.mergeabilityCurrent), ObservedAt: at, MergeCommit: commit,
	})
}
