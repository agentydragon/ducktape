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
			runs, err := p.readCheckRuns(ctx, key.Subject, fences)
			if err != nil {
				return p.refreshFailed(ctx, lease, err)
			}
			return p.state.CompleteCheckRuns(ctx, lease, fences, runs)
		case githubmodel.EventStatus:
			statuses, err := p.readCommitStatuses(ctx, key.Subject, fences)
			if err != nil {
				return p.refreshFailed(ctx, lease, err)
			}
			return p.state.CompleteCommitStatuses(ctx, lease, fences, statuses)
		default:
			return fmt.Errorf("unsupported commit resource %q", key.Event)
		}
	})
}

type commitTarget struct {
	repository     githubmodel.Repository
	sha            string
	installationID int64
}

func (p *Provider) commitTargets(ctx context.Context, subject githubstate.SubjectKey, fences []githubstate.AccessFence) ([]commitTarget, error) {
	revisions, err := p.state.CommitTargets(ctx, subject)
	if err != nil {
		return nil, err
	}
	var targets []commitTarget
	for _, revision := range revisions {
		for _, fence := range fences {
			if fence.Key.RepositoryID == revision.Repository.ID {
				targets = append(targets, commitTarget{repository: revision.Repository, sha: revision.SHA, installationID: fence.Key.InstallationID})
				break
			}
		}
	}
	return targets, nil
}
