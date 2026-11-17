package model

import (
	"errors"
	"fmt"
)

// PullRequestState uses GitHub's PullRequestState vocabulary.
type PullRequestState string

const (
	PullRequestOpen   PullRequestState = "OPEN"
	PullRequestClosed PullRequestState = "CLOSED"
	PullRequestMerged PullRequestState = "MERGED"
)

// MergeableState uses GitHub's MergeableState vocabulary.
type MergeableState string

const (
	Mergeable           MergeableState = "MERGEABLE"
	Conflicting         MergeableState = "CONFLICTING"
	MergeabilityUnknown MergeableState = "UNKNOWN"
)

// MergeCommit is the successful test merge and the exact revisions in its parent pair.
type MergeCommit struct {
	SHA     string `json:"sha"`
	BaseSHA string `json:"baseSha"`
	HeadSHA string `json:"headSha"`
}

// PullRequestObservation records observed refs and GitHub's mergeability result.
// An open MERGEABLE result also requires a test merge whose parents match those refs.
type PullRequestObservation struct {
	BaseRef      string
	HeadSHA      string
	BaseSHA      string
	State        PullRequestState
	Mergeability MergeableState
	MergeCommit  *MergeCommit
}

// Validate checks GitHub's pull request vocabulary and the parent proof for a positive mergeability result.
func (o PullRequestObservation) Validate() error {
	switch o.State {
	case PullRequestOpen, PullRequestClosed, PullRequestMerged:
	default:
		return fmt.Errorf("invalid pull request state %q", o.State)
	}
	switch o.Mergeability {
	case Mergeable, Conflicting, MergeabilityUnknown:
	default:
		return fmt.Errorf("invalid pull request mergeability %q", o.Mergeability)
	}
	if proof := o.MergeCommit; proof != nil {
		if o.State != PullRequestOpen || o.Mergeability != Mergeable ||
			!IsCommitSHA(proof.SHA) || !IsCommitSHA(proof.BaseSHA) || !IsCommitSHA(proof.HeadSHA) ||
			proof.BaseSHA != o.BaseSHA || proof.HeadSHA != o.HeadSHA {
			return errors.New("test merge proof does not match the open mergeable observation")
		}
	} else if o.State == PullRequestOpen && o.Mergeability == Mergeable {
		return errors.New("an open mergeable observation requires matching test merge proof")
	}
	return nil
}
