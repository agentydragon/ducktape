package model

import (
	"cmp"
	"errors"
	"slices"

	"github.com/google/uuid"
)

// Subscribe holds the immutable creation parameters compared on an identical retry.
type Subscribe struct {
	DestinationRef DestinationRef
	SessionID      string
	IdempotencyKey string
	Source         Source
	LifetimeDays   int64
}

// Source is where a subscription's events come from: an ActionsSource or a GitHubSource.
type Source interface{ isSource() }

// ActionsSource follows the events of one Action request.
type ActionsSource struct {
	RequestID uuid.UUID
	// AfterSequence is the request's own event sequence to replay after, not an inbox cursor.
	AfterSequence int64
}

// GitHubSource follows one GitHub subject.
type GitHubSource struct {
	Repository string
	Subject    GitHubSubject
	// Events are the filters as a set: nil selects the subject's defaults, and order and repeats are not part of
	// the subscription's identity.
	Events []EventFilter
}

func (ActionsSource) isSource() {}
func (GitHubSource) isSource()  {}

// GitHubSubject is what a GitHub subscription follows.
type GitHubSubject interface{ isGitHubSubject() }

type (
	PullRequestSubject struct{ Number int64 }
	IssueSubject       struct{ Number int64 }
	BranchSubject      struct{ Name string }
	CommitSubject      struct{ SHA string }
)

func (PullRequestSubject) isGitHubSubject() {}
func (IssueSubject) isGitHubSubject()       {}
func (BranchSubject) isGitHubSubject()      {}
func (CommitSubject) isGitHubSubject()      {}

// EventName is a GitHub event selector this service accepts; some selectors name Agentplane observations.
type EventName string

const (
	EventPullRequest              EventName = "pull_request"
	EventIssueComment             EventName = "issue_comment"
	EventPullRequestReview        EventName = "pull_request_review"
	EventPullRequestReviewComment EventName = "pull_request_review_comment"
	EventCheckRun                 EventName = "check_run"
	EventCheckSuite               EventName = "check_suite"
	EventStatus                   EventName = "status"
	EventPush                     EventName = "push"
	EventCreate                   EventName = "create"
	EventDelete                   EventName = "delete"
	EventWorkflowRun              EventName = "workflow_run"
	EventWorkflowJob              EventName = "workflow_job"
	EventIssues                   EventName = "issues"
	// EventMergeabilityChanged is an Agentplane observation of a pull request's mergeability, not a GitHub webhook.
	EventMergeabilityChanged EventName = "mergeability_changed"
)

var (
	ciEvents        = []EventName{EventCheckRun, EventCheckSuite, EventStatus, EventWorkflowRun, EventWorkflowJob}
	webhookPREvents = []EventName{EventPullRequest, EventIssueComment, EventPullRequestReview, EventPullRequestReviewComment}
	prEvents        = slices.Concat(webhookPREvents, []EventName{EventMergeabilityChanged})
	issueEvents     = []EventName{EventIssues, EventIssueComment}
	refEvents       = []EventName{EventPush, EventCreate, EventDelete}
	// knownEvents are GitHub webhook event names. The generated mergeability selector is deliberately excluded:
	// it has its own identity and can never be decoded as a webhook delivery.
	knownEvents = slices.Concat(ciEvents, webhookPREvents, issueEvents, refEvents)
)

// EventFilter selects one event name, and optionally some of its `action` values.
type EventFilter struct {
	Event EventName
	// Actions is a set: nil selects every action.
	Actions []string
}

func (r DestinationRef) Validate() error {
	return errors.Join(
		CheckText("destination_ref.namespace", r.Namespace, 1, 63),
		CheckText("destination_ref.name", r.Name, 1, 63),
		CheckText("destination_ref.uid", r.UID, 1, 128),
	)
}

// Validate checks the bounds and patterns of the creation.
func (s Subscribe) Validate() error {
	if err := errors.Join(
		s.DestinationRef.Validate(),
		CheckText("session_id", s.SessionID, 1, 200),
		CheckText("idempotency_key", s.IdempotencyKey, 1, 200),
	); err != nil {
		return err
	}
	if s.LifetimeDays < 1 || s.LifetimeDays > 30 {
		return Invalid("lifetime_days must be 1 to 30")
	}
	switch source := s.Source.(type) {
	case ActionsSource:
		if source.AfterSequence < 0 || source.AfterSequence > 1<<31-1 {
			return Invalid("after_sequence must be 0 to 2147483647")
		}
		return nil
	case GitHubSource:
		return source.validate()
	default:
		return Invalid("source must be an Action request or a GitHub subject")
	}
}

func (s GitHubSource) validate() error {
	if err := CheckText("repository", s.Repository, 1, 200); err != nil {
		return err
	}
	if !repositoryPattern.MatchString(s.Repository) {
		return Invalid("repository must be owner/name")
	}
	allowed := slices.Clone(ciEvents)
	switch subject := s.Subject.(type) {
	case PullRequestSubject:
		if subject.Number <= 0 {
			return Invalid("pull request number must be positive")
		}
		allowed = append(allowed, prEvents...)
	case IssueSubject:
		if subject.Number <= 0 {
			return Invalid("issue number must be positive")
		}
		allowed = issueEvents
	case BranchSubject:
		if err := CheckText("branch name", subject.Name, 1, 255); err != nil {
			return err
		}
		allowed = append(allowed, refEvents...)
	case CommitSubject:
		if !shaPattern.MatchString(subject.SHA) {
			return Invalid("commit SHA must be 40 lowercase hexadecimal characters")
		}
	default:
		return Invalid("subject must be a pull request, issue, branch or commit")
	}
	if s.Events == nil {
		return nil
	}
	for _, filter := range s.Events {
		if filter.Actions != nil && len(filter.Actions) == 0 {
			return Invalid("an event filter's actions must hold at least one action")
		}
	}
	filters := NormalizeFilters(s.Events)
	if len(filters) < 1 || len(filters) > 32 {
		return Invalid("events must hold 1 to 32 filters")
	}
	for _, filter := range filters {
		if !slices.Contains(allowed, filter.Event) {
			return Invalid("event %q is not supported for this GitHub subject", filter.Event)
		}
		if filter.Event == EventMergeabilityChanged && filter.Actions != nil {
			return Invalid("event %q does not accept actions", filter.Event)
		}
		if filter.Actions == nil {
			continue
		}
		if len(filter.Actions) > 32 {
			return Invalid("an event filter holds at most 32 actions")
		}
		if slices.Contains(refEvents, filter.Event) || filter.Event == EventStatus {
			return Invalid("event %q has no action selector", filter.Event)
		}
		for _, action := range filter.Actions {
			if len(action) > 64 || !actionPattern.MatchString(action) {
				return Invalid("an action is 1 to 64 lowercase letters and underscores")
			}
		}
	}
	return nil
}

// NormalizeFilters orders the filter set by event name and then actions, sorting and deduplicating each action set.
func NormalizeFilters(filters []EventFilter) []EventFilter {
	normalized := make([]EventFilter, len(filters))
	for i, filter := range filters {
		normalized[i] = EventFilter{Event: filter.Event, Actions: normalizeActions(filter.Actions)}
	}
	slices.SortFunc(normalized, func(a, b EventFilter) int {
		return cmp.Or(cmp.Compare(a.Event, b.Event), slices.Compare(a.Actions, b.Actions))
	})
	return slices.CompactFunc(normalized, func(a, b EventFilter) bool {
		return a.Event == b.Event && (a.Actions == nil) == (b.Actions == nil) && slices.Equal(a.Actions, b.Actions)
	})
}

func normalizeActions(actions []string) []string {
	if actions == nil {
		return nil
	}
	sorted := slices.Clone(actions)
	slices.Sort(sorted)
	return slices.Compact(sorted)
}

// Equal reports whether two creations name the same subscription: every field, with event and action filters
// compared as sets.
func (s Subscribe) Equal(other Subscribe) bool {
	return s.DestinationRef == other.DestinationRef && s.SessionID == other.SessionID &&
		s.IdempotencyKey == other.IdempotencyKey && s.LifetimeDays == other.LifetimeDays &&
		SourcesEqual(s.Source, other.Source)
}

// SourcesEqual reports whether two source selections have the same identity.
func SourcesEqual(a, b Source) bool {
	switch a := a.(type) {
	case ActionsSource:
		b, ok := b.(ActionsSource)
		return ok && a == b
	case GitHubSource:
		b, ok := b.(GitHubSource)
		if !ok || a.Repository != b.Repository || a.Subject != b.Subject || (a.Events == nil) != (b.Events == nil) {
			return false
		}
		return slices.EqualFunc(NormalizeFilters(a.Events), NormalizeFilters(b.Events), func(x, y EventFilter) bool {
			return x.Event == y.Event && (x.Actions == nil) == (y.Actions == nil) && slices.Equal(x.Actions, y.Actions)
		})
	default:
		return false
	}
}
