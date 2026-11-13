package github

import (
	"context"

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

func (p *Provider) readCheckRuns(ctx context.Context, subject githubstate.SubjectKey, fences []githubstate.AccessFence) ([]model.GitHubCheckRun, error) {
	targets, err := p.commitTargets(ctx, subject, fences)
	if err != nil {
		return nil, err
	}
	observations := make([]model.GitHubCheckRun, 0)
	for _, target := range targets {
		runs, err := p.client.CheckRuns(ctx, target.repository.Name, target.sha, target.installationID)
		if err != nil {
			return nil, err
		}
		for i := range runs {
			runs[i].RepositoryID = target.repository.ID
		}
		observations = append(observations, runs...)
	}
	return observations, nil
}
