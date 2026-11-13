package github

import (
	"context"
	"errors"
	"fmt"
	"time"

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// Each commit resource has its own deadline and error state. Pending mergeability retries do not accelerate
// either read, and a classified failure in one resource does not stop the other's observations or raw activity.
func (p *Provider) refreshCommitResource(ctx context.Context, key githubstate.CommitRefreshKey, fences []githubstate.AccessFence) (*time.Time, error) {
	lease, err := p.state.Acquire(ctx, key)
	var deferred *githubstate.RefreshDeferredError
	if errors.As(err, &deferred) {
		return &deferred.Until, nil
	}
	if err != nil {
		return nil, err
	}
	if lease != nil {
		switch key.Event {
		case model.EventCheckRun:
			runs, readErr := p.readCheckRuns(ctx, key.Subject, fences)
			if readErr != nil {
				err = p.refreshFailed(ctx, *lease, readErr)
			} else {
				err = p.state.CompleteCheckRuns(ctx, *lease, fences, runs)
			}
		case model.EventStatus:
			statuses, readErr := p.readCommitStatuses(ctx, key.Subject, fences)
			if readErr != nil {
				err = p.refreshFailed(ctx, *lease, readErr)
			} else {
				err = p.state.CompleteCommitStatuses(ctx, *lease, fences, statuses)
			}
		default:
			return nil, fmt.Errorf("unsupported commit resource %q", key.Event)
		}
		if errors.As(err, &deferred) {
			return &deferred.Until, nil
		}
		if err != nil {
			return nil, err
		}
	}
	state, err := p.state.CommitState(ctx, key)
	return state.RetryAt, err
}

type commitTarget struct {
	repository     model.GitHubRepository
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
