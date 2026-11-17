package sourceadapter

import (
	"context"
	"fmt"

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes shared GitHub state from adapter status variants.
	"github.com/google/uuid"
)

// SubscriptionStatus is the closed set of source-owned subscription projections.
type SubscriptionStatus interface{ isSubscriptionStatus() }

type (
	ActionsStatus struct{}
	GitHubStatus  struct{ Status githubstate.Status }
)

func (ActionsStatus) isSubscriptionStatus() {}
func (GitHubStatus) isSubscriptionStatus()  {}

func LoadSubscriptionStatus(ctx context.Context, q githubstate.Querier, id uuid.UUID, source Source) (SubscriptionStatus, error) {
	switch source.(type) {
	case ActionsSource:
		return ActionsStatus{}, nil
	case GitHubSource:
		status, err := githubstate.LoadSubscriptionStatus(ctx, q, id)
		if err != nil {
			return nil, err
		}
		return GitHubStatus{Status: status}, nil
	default:
		return nil, fmt.Errorf("subscription %s: unknown source %T", id, source)
	}
}
