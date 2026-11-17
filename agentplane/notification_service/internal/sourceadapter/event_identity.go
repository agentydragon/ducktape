package sourceadapter

import (
	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// EventIdentity is the closed set of source event identities accepted by inbox storage.
type EventIdentity interface{ isEventIdentity() }

type (
	ActionsEvent           struct{ model.ActionsEvent }
	GitHubEvent            struct{ githubmodel.GitHubEvent }
	GitHubObservationEvent struct {
		githubmodel.GitHubObservationEvent
	}
	GitHubMergeabilityEvent struct {
		githubmodel.GitHubMergeabilityEvent
	}
	GitHubMergeabilitySnapshotEvent struct {
		githubmodel.GitHubMergeabilitySnapshotEvent
	}
	MessageEvent struct{ model.MessageEvent }
)

func (ActionsEvent) isEventIdentity()                    {}
func (GitHubEvent) isEventIdentity()                     {}
func (GitHubObservationEvent) isEventIdentity()          {}
func (GitHubMergeabilityEvent) isEventIdentity()         {}
func (GitHubMergeabilitySnapshotEvent) isEventIdentity() {}
func (MessageEvent) isEventIdentity()                    {}

func WrapActionsEvent(value model.ActionsEvent) ActionsEvent    { return ActionsEvent{value} }
func WrapGitHubEvent(value githubmodel.GitHubEvent) GitHubEvent { return GitHubEvent{value} }
func WrapGitHubObservationEvent(value githubmodel.GitHubObservationEvent) GitHubObservationEvent {
	return GitHubObservationEvent{value}
}

func WrapGitHubMergeabilityEvent(value githubmodel.GitHubMergeabilityEvent) GitHubMergeabilityEvent {
	return GitHubMergeabilityEvent{value}
}

func WrapGitHubMergeabilitySnapshotEvent(value githubmodel.GitHubMergeabilitySnapshotEvent) GitHubMergeabilitySnapshotEvent {
	return GitHubMergeabilitySnapshotEvent{value}
}
func WrapMessageEvent(value model.MessageEvent) MessageEvent { return MessageEvent{value} }
