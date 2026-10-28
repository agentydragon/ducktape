package client

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/google/go-github/v88/github"
)

// Repository is a repository's stable identity.
type Repository struct {
	ID       int64
	FullName string
}

func splitRepository(repository string) (owner, name string, err error) {
	if !model.IsRepositoryName(repository) {
		return "", "", errors.New("repository must be owner/name")
	}
	owner, name, _ = strings.Cut(repository, "/")
	return owner, name, nil
}

func repositoryFrom(repository *github.Repository) (Repository, error) {
	if repository.GetID() <= 0 || !model.IsRepositoryName(repository.GetFullName()) {
		return Repository{}, &InvalidResponseError{Reason: "repository identity is missing or malformed"}
	}
	return Repository{ID: repository.GetID(), FullName: repository.GetFullName()}, nil
}

// Installation returns the ID of the App's installation for a repository. It is a NotInstalledError if the App has
// none that can see it, and a SuspendedError if the installation is suspended.
func (c *Client) Installation(ctx context.Context, repository string) (int64, error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return 0, err
	}
	installation, err := callAsApp(ctx, c, true, func(ctx context.Context, client *github.Client) (*github.Installation, *github.Response, error) {
		return client.Apps.GetRepositoryInstallation(ctx, owner, name)
	})
	switch {
	case errors.Is(err, errMissing):
		return 0, &NotInstalledError{AccessError: AccessError{StatusCode: http.StatusNotFound}}
	case err != nil:
		return 0, err
	case installation.GetID() <= 0:
		return 0, &InvalidResponseError{Reason: "installation id is missing"}
	case installation.SuspendedAt != nil:
		return 0, &SuspendedError{AccessError: AccessError{StatusCode: http.StatusOK}}
	}
	return installation.GetID(), nil
}

// Repository reads a repository by name as an installation.
func (c *Client) Repository(ctx context.Context, name string, installationID int64) (Repository, error) {
	owner, repo, err := splitRepository(name)
	if err != nil {
		return Repository{}, err
	}
	repository, err := callAsInstallation(ctx, c, installationID, false, func(ctx context.Context, client *github.Client) (*github.Repository, *github.Response, error) {
		return client.Repositories.Get(ctx, owner, repo)
	})
	if err != nil {
		return Repository{}, err
	}
	return repositoryFrom(repository)
}

// RepositoryByID reads a repository by its numeric ID as an installation.
func (c *Client) RepositoryByID(ctx context.Context, id, installationID int64) (Repository, error) {
	repository, err := callAsInstallation(ctx, c, installationID, false, func(ctx context.Context, client *github.Client) (*github.Repository, *github.Response, error) {
		return client.Repositories.GetByID(ctx, id)
	})
	if err != nil {
		return Repository{}, err
	}
	return repositoryFrom(repository)
}
