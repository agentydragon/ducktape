package storagecodec

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// MergeabilityEntryContentStorageFor derives native mergeability columns from their canonical JSON payload.
func MergeabilityEntryContentStorageFor(payload json.RawMessage, identityNumber int64) (storagecodec.EntryContentStorage, error) {
	var c storagecodec.EntryContentStorage
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
	if err := storagecodec.Decode(payload, &wire); err != nil {
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

// MergeabilityPayloadFromStorage reconstructs a mergeability payload from its native content columns.
func MergeabilityPayloadFromStorage(c storagecodec.EntryContentStorage, number int64) (json.RawMessage, error) {
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

// GitHubPayloadFromStorage returns native JSON for a webhook or current-state observation.
func GitHubPayloadFromStorage(payload []byte) (json.RawMessage, error) {
	if payload == nil {
		return nil, errors.New("GitHub payload expired before the inbox's expired prefix")
	}
	return json.RawMessage(payload), nil
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
