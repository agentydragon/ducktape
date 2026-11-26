package client

import (
	"context"
	"iter"
	"time"

	"github.com/google/go-github/v88/github"
)

// pageSize is the page size every paginated read asks for, the most GitHub's REST API returns.
const pageSize = 100

// installationPages gives each REST page its own authenticated request and timeout.
func installationPages[T any](ctx context.Context, c *Client, installationID int64, profile installationPermissionProfile,
	resource string, read func(context.Context, *github.Client, github.ListOptions) (T, *github.Response, error),
) iter.Seq2[T, error] {
	return func(yield func(T, error) bool) {
		opts := github.ListOptions{Page: 1, PerPage: pageSize}
		for {
			value, response, err := callAsInstallationWithProfile(ctx, c, installationID, profile, false, func(ctx context.Context, client *github.Client) (T, *github.Response, error) {
				return read(ctx, client, opts)
			})
			if err != nil {
				yield(value, err)
				return
			}
			if response == nil {
				yield(value, &InvalidResponseError{Reason: resource + " response is missing"})
				return
			}
			if !yield(value, nil) || response.NextPage == 0 {
				return
			}
			if response.NextPage <= opts.Page {
				var zero T
				yield(zero, &InvalidResponseError{Reason: resource + " pagination did not advance"})
				return
			}
			opts.Page = response.NextPage
		}
	}
}

func timestampTime(timestamp *github.Timestamp) *time.Time {
	if timestamp == nil {
		return nil
	}
	value := timestamp.Time.UTC()
	return &value
}
