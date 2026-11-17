package github

import (
	"context"
	"fmt"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
)

// refreshSubject refreshes the subject unless it is fresh or another refresh holds it: the commits it is at, in
// the repositories holding them, and the grant on a pull request's fork when the App is installed there.
func (p *Provider) refreshSubject(ctx context.Context, source githubmodel.GitHubSource, subject githubstate.SubjectKey, base githubstate.AccessFence) error {
	lease, err := p.state.Acquire(ctx, subject)
	if err != nil || lease == nil {
		return err
	}
	observation, err := p.readSubject(ctx, source, subject, base.Key.InstallationID)
	if err != nil {
		return p.refreshFailed(ctx, *lease, err)
	}
	return p.state.CompleteSubject(ctx, *lease, []githubstate.AccessFence{base}, githubstate.SubjectObservation{Revisions: observation.revisions, PullRequest: observation.pullRequest})
}

type subjectObservation struct {
	revisions   []githubmodel.GitHubHeadRevision
	pullRequest *githubmodel.GitHubPullRequestObservation
}

func (p *Provider) readSubject(ctx context.Context, source githubmodel.GitHubSource, subject githubstate.SubjectKey, installation int64) (subjectObservation, error) {
	name, err := p.state.RepositoryName(ctx, subject.RepositoryID)
	if err != nil {
		return subjectObservation{}, err
	}
	here := githubmodel.GitHubRepository{ID: subject.RepositoryID, Name: name}
	switch s := source.Subject.(type) {
	case githubmodel.PullRequestSubject:
		pullRequest, err := p.client.PullRequest(ctx, name, int(s.Number), installation)
		if err != nil {
			return subjectObservation{}, err
		}
		revisions := []githubmodel.GitHubHeadRevision{{Repository: here, SHA: pullRequest.Head.SHA}}
		// Base-repository CI may build the fork's head, so the head is associated with the fork by the identity
		// GitHub names, never by an equal SHA. Only an installation on the fork grants access to its deliveries.
		if pullRequest.Head.Repository != nil && pullRequest.Head.Repository.ID != subject.RepositoryID {
			fork := githubmodel.GitHubRepository{ID: pullRequest.Head.Repository.ID, Name: pullRequest.Head.Repository.FullName}
			revisions = append(revisions, githubmodel.GitHubHeadRevision{Repository: fork, SHA: pullRequest.Head.SHA})
			access, err := p.forkAccess(ctx, *pullRequest.Head.Repository)
			if err != nil {
				return subjectObservation{}, err
			}
			if access != nil {
				if err := p.state.Grant(ctx, *access, fork.Name); err != nil {
					return subjectObservation{}, err
				}
			}
		}
		return subjectObservation{
			revisions: revisions,
			pullRequest: &githubmodel.GitHubPullRequestObservation{
				BaseRef: pullRequest.BaseRef, HeadSHA: pullRequest.Head.SHA, BaseSHA: pullRequest.BaseSHA,
				State: pullRequest.State, Mergeability: pullRequest.Mergeability, MergeCommit: pullRequest.MergeCommit,
			},
		}, nil
	case githubmodel.IssueSubject:
		return subjectObservation{}, p.client.CheckIssue(ctx, name, int(s.Number), installation)
	case githubmodel.BranchSubject:
		sha, found, err := p.client.Branch(ctx, name, s.Name, installation)
		if err != nil || !found {
			return subjectObservation{}, err
		}
		return subjectObservation{revisions: []githubmodel.GitHubHeadRevision{{Repository: here, SHA: sha}}}, nil
	case githubmodel.CommitSubject:
		if err := p.client.CheckCommit(ctx, name, s.SHA, installation); err != nil {
			return subjectObservation{}, err
		}
		return subjectObservation{revisions: []githubmodel.GitHubHeadRevision{{Repository: here, SHA: s.SHA}}}, nil
	default:
		return subjectObservation{}, fmt.Errorf("unsupported GitHub subject %T", s)
	}
}
