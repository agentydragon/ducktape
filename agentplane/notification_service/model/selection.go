package model

import "slices"

// GitHubCommitObservationSelection is the native fact set selected by a GitHub source.
type GitHubCommitObservationSelection struct {
	CheckRuns              bool
	CompletedCheckRunsOnly bool
	CommitStatuses         bool
}

// Filters returns the named event filters, or the defaults for the subject when Events is nil.
func (s GitHubSource) Filters() []EventFilter {
	if s.Events != nil {
		return s.Events
	}
	if _, ok := s.Subject.(IssueSubject); ok {
		return unfiltered(issueEvents)
	}
	filters := []EventFilter{{Event: EventStatus}, {Event: EventCheckRun, Actions: []string{"completed"}}}
	switch s.Subject.(type) {
	case PullRequestSubject:
		filters = append(filters, unfiltered(prEvents)...)
	case BranchSubject:
		filters = append(filters, unfiltered(refEvents)...)
	}
	return filters
}

func unfiltered(events []EventName) []EventFilter {
	filters := make([]EventFilter, len(events))
	for i, event := range events {
		filters[i] = EventFilter{Event: event}
	}
	return filters
}

// CommitObservationSelection reports which generated commit facts a PR source selects.
func (s GitHubSource) CommitObservationSelection() GitHubCommitObservationSelection {
	if _, ok := s.Subject.(PullRequestSubject); !ok {
		return GitHubCommitObservationSelection{}
	}
	selection := GitHubCommitObservationSelection{CompletedCheckRunsOnly: s.Events == nil}
	for _, filter := range s.Filters() {
		switch filter.Event {
		case EventCheckRun:
			if s.Events == nil || filter.Actions == nil {
				selection.CheckRuns = true
			}
		case EventStatus:
			selection.CommitStatuses = true
		}
	}
	return selection
}

// CheckRunObservationsSelected reports whether generated check-run facts are selected. Explicit action filters
// select only real webhook actions and leave check-run facts as ordinary raw deliveries.
func (s GitHubSource) CheckRunObservationsSelected() bool {
	return s.CommitObservationSelection().CheckRuns
}

// CommitStatusObservationsSelected reports whether generated commit-status facts are selected.
func (s GitHubSource) CommitStatusObservationsSelected() bool {
	return s.CommitObservationSelection().CommitStatuses
}

// GitHubCIEvents returns the GitHub events that can match a commit's CI observations.
func GitHubCIEvents() []EventName {
	return slices.Clone(ciEvents)
}

// IsGitHubWebhookEvent reports whether event is a supported native GitHub webhook name.
// Agentplane-generated observation selectors, such as mergeability_changed, are excluded.
func IsGitHubWebhookEvent(event EventName) bool {
	return slices.Contains(knownEvents, event)
}
