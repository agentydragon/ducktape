package github

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
)

func (p *Provider) readCheckRuns(ctx context.Context, subject store.GitHubSubjectKey, fences []store.GitHubAccessFence) ([]model.GitHubCheckRun, error) {
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
