package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/agentydragon/ducktape/agentplane/notification_service/entrystoragepb"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type storedEntryContent struct {
	typedPayload       []byte
	messageBody        *string
	messageFromSession *string
}

// marshalEntryStorageContent retains the typed payload and exposes Message's canonical text columns from the same
// decoded value. The text columns are dual-written with the legacy JSON and protobuf until the reader cutover.
func marshalEntryStorageContent(event model.EventIdentity, payload json.RawMessage) (storedEntryContent, error) {
	var content *entrystoragepb.EntryContent
	var stored storedEntryContent
	switch event := event.(type) {
	case model.ActionsEvent:
		var action ActionEvent
		if err := json.Unmarshal(payload, &action); err != nil {
			return storedEntryContent{}, fmt.Errorf("decode Action content: %w", err)
		}
		if action.Sequence != event.Sequence {
			return storedEntryContent{}, fmt.Errorf("Action content sequence %d does not match identity sequence %d", action.Sequence, event.Sequence)
		}
		at := timestamppb.New(action.At)
		if err := at.CheckValid(); err != nil {
			return storedEntryContent{}, fmt.Errorf("Action content at: %w", err)
		}
		content = &entrystoragepb.EntryContent{Content: &entrystoragepb.EntryContent_Action{Action: &entrystoragepb.ActionContent{
			State: action.State, At: at, Actor: action.Actor,
		}}}
	case model.GitHubEvent:
		return storedEntryContent{}, nil
	case model.GitHubObservationEvent:
		return storedEntryContent{}, nil
	case model.GitHubMergeabilityEvent:
		mergeability, err := mergeabilityContentFromJSON(payload, event.PullRequestNumber)
		if err != nil {
			return storedEntryContent{}, err
		}
		content = &entrystoragepb.EntryContent{Content: &entrystoragepb.EntryContent_Mergeability{Mergeability: mergeability}}
	case model.GitHubMergeabilitySnapshotEvent:
		mergeability, err := mergeabilityContentFromJSON(payload, event.PullRequestNumber)
		if err != nil {
			return storedEntryContent{}, err
		}
		content = &entrystoragepb.EntryContent{Content: &entrystoragepb.EntryContent_Mergeability{Mergeability: mergeability}}
	case model.MessageEvent:
		var wire struct {
			Body        *string `json:"body"`
			FromSession *string `json:"from_session"`
		}
		if err := decode(payload, &wire); err != nil {
			return storedEntryContent{}, fmt.Errorf("decode message content: %w", err)
		}
		if wire.Body == nil {
			return storedEntryContent{}, errors.New("message content has no body")
		}
		stored.messageBody = wire.Body
		stored.messageFromSession = wire.FromSession
		message := &entrystoragepb.MessageContent{Body: *wire.Body}
		if wire.FromSession != nil {
			message.FromSession = wire.FromSession
		}
		content = &entrystoragepb.EntryContent{Content: &entrystoragepb.EntryContent_Message{Message: message}}
	default:
		return storedEntryContent{}, fmt.Errorf("unsupported event identity type %T", event)
	}
	encoded, err := proto.Marshal(content)
	if err != nil {
		return storedEntryContent{}, fmt.Errorf("marshal typed entry content: %w", err)
	}
	stored.typedPayload = encoded
	return stored, nil
}

// entryPayloadFromStoredContent recreates the public JSON payload from typed storage. GitHub webhook payloads are
// already native JSON; the other providers have no legacy JSON fallback after their backfill gate completes.
func entryPayloadFromStoredContent(event model.EventIdentity, legacyPayload, typedPayload []byte, legacyPayloadPresent bool) (json.RawMessage, error) {
	switch event.(type) {
	case model.GitHubEvent, model.GitHubObservationEvent:
		if typedPayload != nil {
			return nil, errors.New("GitHub webhook has an unexpected typed payload")
		}
		if !legacyPayloadPresent || legacyPayload == nil {
			return nil, errors.New("payload expired before the inbox's expired prefix")
		}
		return json.RawMessage(legacyPayload), nil
	}
	if typedPayload == nil {
		if !legacyPayloadPresent {
			return nil, errors.New("payload expired before the inbox's expired prefix")
		}
		return nil, errors.New("typed content is missing; complete the entry content backfill before deploying this reader")
	}
	var content entrystoragepb.EntryContent
	if err := proto.Unmarshal(typedPayload, &content); err != nil {
		return nil, fmt.Errorf("decode typed entry content: %w", err)
	}
	switch event := event.(type) {
	case model.ActionsEvent:
		action := content.GetAction()
		if action == nil || action.GetAt() == nil {
			return nil, errors.New("typed Action content is incomplete")
		}
		if err := action.GetAt().CheckValid(); err != nil {
			return nil, fmt.Errorf("typed Action content at: %w", err)
		}
		payload := ActionEvent{
			Sequence: event.Sequence, State: action.GetState(), At: action.GetAt().AsTime(), Actor: action.GetActor(),
		}
		encoded, err := json.Marshal(payload)
		if err != nil {
			return nil, fmt.Errorf("encode Action JSON payload: %w", err)
		}
		return encoded, nil
	case model.GitHubMergeabilityEvent:
		return mergeabilityPayloadFromStoredContent(content.GetMergeability(), event.PullRequestNumber)
	case model.GitHubMergeabilitySnapshotEvent:
		return mergeabilityPayloadFromStoredContent(content.GetMergeability(), event.PullRequestNumber)
	case model.MessageEvent:
		message := content.GetMessage()
		if message == nil {
			return nil, errors.New("typed message content is incomplete")
		}
		encoded, err := json.Marshal(struct {
			Body        string  `json:"body"`
			FromSession *string `json:"from_session,omitempty"`
		}{Body: message.GetBody(), FromSession: message.FromSession})
		if err != nil {
			return nil, fmt.Errorf("encode message JSON payload: %w", err)
		}
		return encoded, nil
	default:
		return nil, fmt.Errorf("unsupported event identity type %T", event)
	}
}

func mergeabilityPayloadFromStoredContent(content *entrystoragepb.MergeabilityContent, pullRequestNumber int64) (json.RawMessage, error) {
	if content == nil || content.GetObservedAt() == nil {
		return nil, errors.New("typed mergeability content is incomplete")
	}
	if err := content.GetObservedAt().CheckValid(); err != nil {
		return nil, fmt.Errorf("typed mergeability observed_at: %w", err)
	}
	previous, err := mergeabilityStateFromStorage(content.GetPreviousMergeability())
	if err != nil {
		return nil, fmt.Errorf("typed previous mergeability: %w", err)
	}
	current, err := mergeabilityStateFromStorage(content.GetMergeability())
	if err != nil {
		return nil, fmt.Errorf("typed mergeability: %w", err)
	}
	var mergeCommit *model.GitHubMergeCommit
	if stored := content.GetMergeCommit(); stored != nil {
		mergeCommit = &model.GitHubMergeCommit{SHA: stored.GetSha(), BaseSHA: stored.GetBaseSha(), HeadSHA: stored.GetHeadSha()}
	}
	encoded, err := json.Marshal(mergeabilityPayload{
		Repository: content.GetRepository(), PullRequestNumber: pullRequestNumber,
		BaseRef: content.GetBaseRef(), HeadSHA: content.GetHeadSha(), BaseSHA: content.GetBaseSha(),
		PreviousMergeability: previous, Mergeability: current, ObservedAt: content.GetObservedAt().AsTime(), MergeCommit: mergeCommit,
	})
	if err != nil {
		return nil, fmt.Errorf("encode mergeability JSON payload: %w", err)
	}
	return encoded, nil
}

func mergeabilityStateFromStorage(state entrystoragepb.GitHubMergeabilityState) (model.GitHubMergeableState, error) {
	switch state {
	case entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_UNKNOWN:
		return model.GitHubMergeabilityUnknown, nil
	case entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_MERGEABLE:
		return model.GitHubMergeable, nil
	case entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_CONFLICTING:
		return model.GitHubConflicting, nil
	default:
		return "", fmt.Errorf("unknown mergeability state %d", state)
	}
}

func mergeabilityContentFromJSON(payload json.RawMessage, identityNumber int64) (*entrystoragepb.MergeabilityContent, error) {
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
		return nil, fmt.Errorf("decode mergeability content: %w", err)
	}
	if wire.Repository == nil || wire.PullRequestNumber == nil || wire.BaseRef == nil || wire.HeadSHA == nil ||
		wire.BaseSHA == nil || wire.PreviousMergeability == nil || wire.Mergeability == nil || wire.ObservedAt == nil {
		return nil, errors.New("mergeability content is incomplete")
	}
	if *wire.PullRequestNumber != identityNumber {
		return nil, fmt.Errorf("mergeability content pull request %d does not match identity number %d", *wire.PullRequestNumber, identityNumber)
	}
	previous, err := mergeabilityStorageState(model.GitHubMergeableState(*wire.PreviousMergeability))
	if err != nil {
		return nil, fmt.Errorf("previous mergeability: %w", err)
	}
	current, err := mergeabilityStorageState(model.GitHubMergeableState(*wire.Mergeability))
	if err != nil {
		return nil, fmt.Errorf("mergeability: %w", err)
	}
	observedAt := timestamppb.New(*wire.ObservedAt)
	if err := observedAt.CheckValid(); err != nil {
		return nil, fmt.Errorf("observed_at: %w", err)
	}
	var mergeCommit *entrystoragepb.GitHubMergeCommitContent
	if wire.MergeCommit != nil {
		mergeCommit = &entrystoragepb.GitHubMergeCommitContent{
			Sha: wire.MergeCommit.SHA, BaseSha: wire.MergeCommit.BaseSHA, HeadSha: wire.MergeCommit.HeadSHA,
		}
	}
	return &entrystoragepb.MergeabilityContent{
		Repository: *wire.Repository, BaseRef: *wire.BaseRef, HeadSha: *wire.HeadSHA, BaseSha: *wire.BaseSHA,
		PreviousMergeability: previous, Mergeability: current, ObservedAt: observedAt, MergeCommit: mergeCommit,
	}, nil
}

func mergeabilityStorageState(state model.GitHubMergeableState) (entrystoragepb.GitHubMergeabilityState, error) {
	switch state {
	case model.GitHubMergeabilityUnknown:
		return entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_UNKNOWN, nil
	case model.GitHubMergeable:
		return entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_MERGEABLE, nil
	case model.GitHubConflicting:
		return entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_CONFLICTING, nil
	default:
		return entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_UNSPECIFIED,
			fmt.Errorf("unknown state %q", state)
	}
}
