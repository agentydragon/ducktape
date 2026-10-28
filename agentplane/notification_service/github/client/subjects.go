package client

import (
	"context"
	"errors"
	"slices"
	"strings"

	"github.com/google/go-github/v88/github"
)

// CheckIssue reads an issue as an installation and returns a SourceChangedError unless it is that issue and not a
// pull request.
func (c *Client) CheckIssue(ctx context.Context, repository string, number int, installationID int64) error {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return err
	}
	issue, err := callAsInstallation(ctx, c, installationID, false, func(ctx context.Context, client *github.Client) (*github.Issue, *github.Response, error) {
		return client.Issues.Get(ctx, owner, name, number)
	})
	switch {
	case err != nil:
		return err
	case issue == nil || issue.Number == nil:
		return &InvalidResponseError{Reason: "issue number is missing"}
	case *issue.Number != number || issue.PullRequestLinks != nil:
		return &SourceChangedError{Reason: "GitHub issue identity changed or refers to a pull request"}
	}
	return nil
}

// Branch reads a branch's head commit as an installation. found is false if the branch does not exist.
func (c *Client) Branch(ctx context.Context, repository, branch string, installationID int64) (sha string, found bool, err error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return "", false, err
	}
	// A segment of only dots is not allowed in a branch name, and the request URL would resolve it onto another
	// endpoint.
	if slices.ContainsFunc(strings.Split(branch, "/"), func(segment string) bool { return segment == "." || segment == ".." }) {
		return "", false, nil
	}
	ref, err := callAsInstallation(ctx, c, installationID, true, func(ctx context.Context, client *github.Client) (*github.Reference, *github.Response, error) {
		return client.Git.GetRef(ctx, owner, name, "heads/"+branch)
	})
	switch {
	case errors.Is(err, errMissing):
		return "", false, nil
	case err != nil:
		return "", false, err
	case ref.GetObject().GetSHA() == "":
		return "", false, &InvalidResponseError{Reason: "branch commit is missing"}
	}
	return ref.GetObject().GetSHA(), true, nil
}

// CheckCommit reads a commit as an installation and returns a SourceChangedError unless it is that commit.
func (c *Client) CheckCommit(ctx context.Context, repository, sha string, installationID int64) error {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return err
	}
	commit, err := callAsInstallation(ctx, c, installationID, false, func(ctx context.Context, client *github.Client) (*github.RepositoryCommit, *github.Response, error) {
		return client.Repositories.GetCommit(ctx, owner, name, sha, nil)
	})
	switch {
	case err != nil:
		return err
	case commit.GetSHA() == "":
		return &InvalidResponseError{Reason: "commit sha is missing"}
	case commit.GetSHA() != sha:
		return &SourceChangedError{Reason: "GitHub commit identity changed"}
	}
	return nil
}
