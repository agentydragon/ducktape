package model

import (
	"errors"
	"fmt"
)

// GitHubPullRequestState uses GitHub's PullRequestState vocabulary.
type GitHubPullRequestState string

const (
	GitHubPullRequestOpen   GitHubPullRequestState = "OPEN"
	GitHubPullRequestClosed GitHubPullRequestState = "CLOSED"
	GitHubPullRequestMerged GitHubPullRequestState = "MERGED"
)

// GitHubMergeableState uses GitHub's MergeableState vocabulary.
type GitHubMergeableState string

const (
	GitHubMergeable           GitHubMergeableState = "MERGEABLE"
	GitHubConflicting         GitHubMergeableState = "CONFLICTING"
	GitHubMergeabilityUnknown GitHubMergeableState = "UNKNOWN"
)

// GitHubMergeCommit is the successful test merge and the exact revisions in its parent pair.
type GitHubMergeCommit struct {
	SHA     string `json:"sha"`
	BaseSHA string `json:"baseSha"`
	HeadSHA string `json:"headSha"`
}

// GitHubPullRequestObservation records observed refs and GitHub's mergeability result.
// An open MERGEABLE result also requires a test merge whose parents match those refs.
type GitHubPullRequestObservation struct {
	BaseRef      string
	HeadSHA      string
	BaseSHA      string
	State        GitHubPullRequestState
	Mergeability GitHubMergeableState
	MergeCommit  *GitHubMergeCommit
}

// Validate checks GitHub's pull request vocabulary and the parent proof for a positive mergeability result.
func (o GitHubPullRequestObservation) Validate() error {
	switch o.State {
	case GitHubPullRequestOpen, GitHubPullRequestClosed, GitHubPullRequestMerged:
	default:
		return fmt.Errorf("invalid pull request state %q", o.State)
	}
	switch o.Mergeability {
	case GitHubMergeable, GitHubConflicting, GitHubMergeabilityUnknown:
	default:
		return fmt.Errorf("invalid pull request mergeability %q", o.Mergeability)
	}
	if proof := o.MergeCommit; proof != nil {
		if o.State != GitHubPullRequestOpen || o.Mergeability != GitHubMergeable ||
			!IsCommitSHA(proof.SHA) || !IsCommitSHA(proof.BaseSHA) || !IsCommitSHA(proof.HeadSHA) ||
			proof.BaseSHA != o.BaseSHA || proof.HeadSHA != o.HeadSHA {
			return errors.New("test merge proof does not match the open mergeable observation")
		}
	} else if o.State == GitHubPullRequestOpen && o.Mergeability == GitHubMergeable {
		return errors.New("an open mergeable observation requires matching test merge proof")
	}
	return nil
}
