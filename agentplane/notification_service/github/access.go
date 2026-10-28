package github

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"github.com/agentydragon/ducktape/agentplane/notification_service/github/client"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
)

// access is the App's access to a repository through the installation that covers it.
func (p *Provider) access(ctx context.Context, repository string) (store.GitHubAccessKey, error) {
	installation, err := p.client.Installation(ctx, repository)
	if err != nil {
		return store.GitHubAccessKey{}, err
	}
	read, err := p.client.Repository(ctx, repository, installation)
	if err != nil {
		return store.GitHubAccessKey{}, err
	}
	return store.GitHubAccessKey{AppID: p.appID, InstallationID: installation, RepositoryID: read.ID}, nil
}

// forkAccess is the App's access to the repository a pull request's head is in, or nil while the App is not
// installed there: an installation on the base repository does not extend to a fork, so a fork the App cannot see
// is left out, not an error. A suspended installation is an error.
func (p *Provider) forkAccess(ctx context.Context, fork client.Repository) (*store.GitHubAccessKey, error) {
	access, err := p.access(ctx, fork.FullName)
	var notInstalled *client.NotInstalledError
	switch {
	case errors.As(err, &notInstalled):
		slog.WarnContext(ctx, "pull request fork is not covered", "fork", fork.FullName)
		return nil, nil
	case err != nil:
		return nil, err
	case access.RepositoryID != fork.ID:
		return nil, &client.SourceChangedError{Reason: "GitHub fork repository identity changed"}
	}
	return &access, nil
}

// Authorize checks that the App can read the source, and returns the access it reads the source under: the
// installation that covers the source's repository. It fails with the client's typed errors.
func (p *Provider) Authorize(ctx context.Context, source model.GitHubSource) (store.GitHubAccessKey, error) {
	access, err := p.access(ctx, source.Repository)
	if err != nil {
		return store.GitHubAccessKey{}, err
	}
	switch subject := source.Subject.(type) {
	case model.PullRequestSubject:
		pullRequest, err := p.client.PullRequest(ctx, source.Repository, int(subject.Number), access.InstallationID)
		if err != nil {
			return store.GitHubAccessKey{}, err
		}
		if pullRequest.Head.Repository != nil && pullRequest.Head.Repository.ID != access.RepositoryID {
			if _, err := p.forkAccess(ctx, *pullRequest.Head.Repository); err != nil {
				return store.GitHubAccessKey{}, err
			}
		}
	case model.IssueSubject:
		err = p.client.CheckIssue(ctx, source.Repository, int(subject.Number), access.InstallationID)
	case model.BranchSubject:
		_, _, err = p.client.Branch(ctx, source.Repository, subject.Name, access.InstallationID)
	case model.CommitSubject:
		err = p.client.CheckCommit(ctx, source.Repository, subject.SHA, access.InstallationID)
	default:
		err = fmt.Errorf("unsupported GitHub subject %T", subject)
	}
	if err != nil {
		return store.GitHubAccessKey{}, err
	}
	return access, nil
}

// refreshAccess returns the fence of a valid access, asking GitHub first if the access needs validating and no
// other refresh holds it.
func (p *Provider) refreshAccess(ctx context.Context, key store.GitHubAccessKey) (store.GitHubAccessFence, error) {
	lease, err := p.state.Acquire(ctx, key)
	if err != nil {
		return store.GitHubAccessFence{}, err
	}
	if lease != nil {
		name, err := p.readAccess(ctx, key)
		if err != nil {
			return store.GitHubAccessFence{}, p.refreshFailed(ctx, *lease, err)
		}
		if err := p.state.CompleteAccess(ctx, *lease, &name); err != nil {
			return store.GitHubAccessFence{}, err
		}
	}
	return p.state.Fence(ctx, key)
}

// readAccess returns the repository's current name if GitHub still shows the repository under the installation.
func (p *Provider) readAccess(ctx context.Context, key store.GitHubAccessKey) (string, error) {
	repository, err := p.client.RepositoryByID(ctx, key.RepositoryID, key.InstallationID)
	if err != nil {
		return "", err
	}
	installation, err := p.client.Installation(ctx, repository.FullName)
	if err != nil {
		return "", err
	}
	if repository.ID != key.RepositoryID || installation != key.InstallationID {
		return "", &client.SourceChangedError{Reason: "GitHub repository/installation identity changed"}
	}
	return repository.FullName, nil
}
