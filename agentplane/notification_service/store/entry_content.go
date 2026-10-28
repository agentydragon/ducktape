package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/entrystoragepb"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// marshalEntryContent writes the non-key content for sources with a private typed storage representation.
// GitHub webhooks are native JSON and remain in entry.payload.
func marshalEntryContent(event EventIdentity, payload json.RawMessage) ([]byte, error) {
	var content *entrystoragepb.EntryContent
	switch event := event.(type) {
	case ActionsEvent:
		var action ActionEvent
		if err := json.Unmarshal(payload, &action); err != nil {
			return nil, fmt.Errorf("decode Action content: %w", err)
		}
		if action.Sequence != event.Sequence {
			return nil, fmt.Errorf("Action content sequence %d does not match identity sequence %d", action.Sequence, event.Sequence)
		}
		at := timestamppb.New(action.At)
		if err := at.CheckValid(); err != nil {
			return nil, fmt.Errorf("Action content at: %w", err)
		}
		content = &entrystoragepb.EntryContent{Content: &entrystoragepb.EntryContent_Action{Action: &entrystoragepb.ActionContent{
			State: action.State, At: at, Actor: action.Actor,
		}}}
	case GitHubEvent:
		return nil, nil
	case GitHubMergeabilityEvent:
		mergeability, err := mergeabilityContentFromJSON(payload, event.PullRequestNumber)
		if err != nil {
			return nil, err
		}
		content = &entrystoragepb.EntryContent{Content: &entrystoragepb.EntryContent_Mergeability{Mergeability: mergeability}}
	case GitHubMergeabilitySnapshotEvent:
		mergeability, err := mergeabilityContentFromJSON(payload, event.PullRequestNumber)
		if err != nil {
			return nil, err
		}
		content = &entrystoragepb.EntryContent{Content: &entrystoragepb.EntryContent_Mergeability{Mergeability: mergeability}}
	case MessageEvent:
		var wire struct {
			Body        *string `json:"body"`
			FromSession *string `json:"from_session"`
		}
		if err := decode(payload, &wire); err != nil {
			return nil, fmt.Errorf("decode message content: %w", err)
		}
		if wire.Body == nil {
			return nil, errors.New("message content has no body")
		}
		message := &entrystoragepb.MessageContent{Body: *wire.Body}
		if wire.FromSession != nil {
			message.FromSession = wire.FromSession
		}
		content = &entrystoragepb.EntryContent{Content: &entrystoragepb.EntryContent_Message{Message: message}}
	default:
		return nil, fmt.Errorf("unsupported event identity type %T", event)
	}
	encoded, err := proto.Marshal(content)
	if err != nil {
		return nil, fmt.Errorf("marshal typed entry content: %w", err)
	}
	return encoded, nil
}

func mergeabilityContentFromJSON(payload json.RawMessage, identityNumber int64) (*entrystoragepb.MergeabilityContent, error) {
	var wire struct {
		Repository           *string    `json:"repository"`
		PullRequestNumber    *int64     `json:"pullRequestNumber"`
		BaseRef              *string    `json:"baseRef"`
		HeadSHA              *string    `json:"headSha"`
		BaseSHA              *string    `json:"baseSha"`
		PreviousMergeability *string    `json:"previousMergeability"`
		Mergeability         *string    `json:"mergeability"`
		ObservedAt           *time.Time `json:"observedAt"`
	}
	if err := decode(payload, &wire); err != nil {
		return nil, fmt.Errorf("decode mergeability content: %w", err)
	}
	if wire.Repository == nil || wire.PullRequestNumber == nil || wire.BaseRef == nil || wire.HeadSHA == nil ||
		wire.BaseSHA == nil || wire.PreviousMergeability == nil || wire.Mergeability == nil || wire.ObservedAt == nil {
		return nil, errors.New("mergeability content is incomplete")
	}
	if *wire.PullRequestNumber != identityNumber {
		return nil, fmt.Errorf("mergeability content pull request %d does not match identity number %d", *wire.PullRequestNumber, identityNumber)
	}
	previous, err := mergeabilityStorageState(GitHubMergeableState(*wire.PreviousMergeability))
	if err != nil {
		return nil, fmt.Errorf("previous mergeability: %w", err)
	}
	current, err := mergeabilityStorageState(GitHubMergeableState(*wire.Mergeability))
	if err != nil {
		return nil, fmt.Errorf("mergeability: %w", err)
	}
	observedAt := timestamppb.New(*wire.ObservedAt)
	if err := observedAt.CheckValid(); err != nil {
		return nil, fmt.Errorf("observed_at: %w", err)
	}
	return &entrystoragepb.MergeabilityContent{
		Repository: *wire.Repository, BaseRef: *wire.BaseRef, HeadSha: *wire.HeadSHA, BaseSha: *wire.BaseSHA,
		PreviousMergeability: previous, Mergeability: current, ObservedAt: observedAt,
	}, nil
}

func mergeabilityStorageState(state GitHubMergeableState) (entrystoragepb.GitHubMergeabilityState, error) {
	switch state {
	case GitHubMergeabilityUnknown:
		return entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_UNKNOWN, nil
	case GitHubMergeable:
		return entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_MERGEABLE, nil
	case GitHubConflicting:
		return entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_CONFLICTING, nil
	default:
		return entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_UNSPECIFIED,
			fmt.Errorf("unknown state %q", state)
	}
}
