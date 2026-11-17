package githubfilters

import (
	"strings"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model"
)

func CommitObservationClause(alias string, selection githubmodel.CommitObservationSelection, add func(any) string) string {
	var selected []string
	if selection.CheckRuns {
		part := alias + ".event = " + add(string(githubmodel.EventCheckRun))
		if selection.CompletedCheckRunsOnly {
			part += " AND " + alias + ".payload->>'status' = " + add(string(githubmodel.CheckRunCompleted))
		}
		selected = append(selected, "("+part+")")
	}
	if selection.CommitStatuses {
		selected = append(selected, "("+alias+".event = "+add(string(githubmodel.EventStatus))+")")
	}
	if len(selected) == 0 {
		return "FALSE"
	}
	return "(" + strings.Join(selected, " OR ") + ")"
}

func PRStateFilterClause(alias string, filters []githubmodel.EventFilter, add func(any) string) string {
	selected := make([]string, 0, len(filters))
	for _, filter := range filters {
		if filter.Event != githubmodel.EventPullRequest && filter.Event != githubmodel.EventPullRequestReview {
			continue
		}
		part := alias + ".event = " + add(string(filter.Event))
		if filter.Actions != nil {
			actions := make([]string, len(filter.Actions))
			for i, action := range filter.Actions {
				actions[i] = add(action)
			}
			part += " AND " + alias + ".payload->>'action' IN (" + strings.Join(actions, ", ") + ")"
		}
		selected = append(selected, "("+part+")")
	}
	if len(selected) == 0 {
		return "FALSE"
	}
	return "(" + strings.Join(selected, " OR ") + ")"
}

func PRStateFilters(source githubmodel.Source) []githubmodel.EventFilter {
	filters := source.Filters()
	selected := make([]githubmodel.EventFilter, 0, len(filters))
	for _, filter := range filters {
		if filter.Event == githubmodel.EventPullRequest || filter.Event == githubmodel.EventPullRequestReview {
			selected = append(selected, filter)
		}
	}
	return selected
}
