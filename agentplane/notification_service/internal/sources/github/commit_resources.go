package github

import (
	"context"
	"fmt"
	"time"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
)

// Each commit resource has its own deadline and error state. Pending mergeability retries do not accelerate
// either read, and a classified failure in one resource does not stop the other's observations or raw activity.
func (p *Provider) refreshCommitResource(ctx context.Context, key githubstate.CommitRefreshKey, fences []githubstate.AccessFence) (*time.Time, error) {
	return p.refreshResource(ctx, key, func(lease githubstate.RefreshLease) error {
		switch key.Event {
		case githubmodel.EventCheckRun:
			return refreshCommitObservations(ctx, p, key.Subject, lease, fences, p.client.CheckRuns, p.state.CompleteCheckRuns)
		case githubmodel.EventStatus:
			return refreshCommitObservations(ctx, p, key.Subject, lease, fences, p.client.CommitStatuses, p.state.CompleteCommitStatuses)
		default:
			return fmt.Errorf("unsupported commit resource %q", key.Event)
		}
	})
}

// Read every covered copy of the head before committing any observations under this resource's lease.
func refreshCommitObservations[T any](ctx context.Context, p *Provider, subject githubstate.SubjectKey, lease githubstate.RefreshLease, fences []githubstate.AccessFence,
	read func(context.Context, githubmodel.HeadRevision, int64) ([]T, error),
	complete func(context.Context, githubstate.RefreshLease, []githubstate.AccessFence, []T) error,
) error {
	revisions, err := p.state.CommitTargets(ctx, subject)
	if err != nil {
		return p.refreshFailed(ctx, lease, err)
	}
	var observations []T
	for _, revision := range revisions {
		for _, fence := range fences {
			if fence.Key.RepositoryID != revision.Repository.ID {
				continue
			}
			batch, err := read(ctx, revision, fence.Key.InstallationID)
			if err != nil {
				return p.refreshFailed(ctx, lease, err)
			}
			observations = append(observations, batch...)
			break
		}
	}
	return complete(ctx, lease, fences, observations)
}
