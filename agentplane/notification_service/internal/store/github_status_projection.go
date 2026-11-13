package store

import (
	"context"
	"fmt"

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// loadGitHubStatus attaches the source-owned shared state to a generic subscription read.
func loadGitHubStatus(ctx context.Context, q githubstate.Querier, subscription *Subscription) error {
	if _, ok := subscription.Source.(model.GitHubSource); !ok {
		return nil
	}
	var app, installation, repository int64
	subject := githubstate.SubjectKey{}
	if err := q.QueryRowContext(ctx, `
		SELECT github_app_id, github_installation_id, github_repository_id, github_subject_kind, github_subject_key
		FROM subscription WHERE id = $1`, subscription.ID).
		Scan(&app, &installation, &repository, &subject.Kind, &subject.Key); err != nil {
		return fmt.Errorf("subscription %s GitHub binding: %w", subscription.ID, err)
	}
	subject.RepositoryID = repository
	status, err := githubstate.LoadStatus(ctx, q, app, installation, subject)
	if err != nil {
		return err
	}
	subscription.GitHub = &status
	return nil
}
