package github

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
)

func (p *Provider) readCommitStatuses(ctx context.Context, subject store.GitHubSubjectKey, fences []store.GitHubAccessFence) ([]model.GitHubCommitStatus, error) {
	targets, err := p.commitTargets(ctx, subject, fences)
	if err != nil {
		return nil, err
	}
	observations := make([]model.GitHubCommitStatus, 0)
	for _, target := range targets {
		statuses, err := p.client.CommitStatuses(ctx, target.repository.Name, target.sha, target.installationID)
		if err != nil {
			return nil, err
		}
		for i := range statuses {
			statuses[i].RepositoryID = target.repository.ID
		}
		observations = append(observations, statuses...)
	}
	return observations, nil
}
