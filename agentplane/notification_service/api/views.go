package api

import (
	"fmt"
	"math"
	"strconv"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// The views below turn what the store reads into the messages the APIs answer with. A row the store holds that no
// message can show is a fault of the service, not of the caller, so each fails with an error that failure
// reports as unavailable.

func timestamp(t *time.Time) *timestamppb.Timestamp {
	if t == nil {
		return nil
	}
	return timestamppb.New(*t)
}

func toInt32(field string, value int64) (int32, error) {
	if value < math.MinInt32 || value > math.MaxInt32 {
		return 0, fmt.Errorf("stored %s %d does not fit the API", field, value)
	}
	return int32(value), nil
}

func failureKind(kind store.SourceFailureKind) (notificationspb.SourceFailureKind, error) {
	switch kind {
	case store.FailureRateLimited:
		return notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_RATE_LIMITED, nil
	case store.FailureUnavailable:
		return notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_UNAVAILABLE, nil
	case store.FailureAccessDenied:
		return notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_ACCESS_DENIED, nil
	case store.FailureSourceChanged:
		return notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_SOURCE_CHANGED, nil
	case store.FailureProcessingError:
		return notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_PROCESSING_ERROR, nil
	default:
		return 0, fmt.Errorf("stored source failure kind %q is unknown", kind)
	}
}

// observation is a source's last success and current failure, as the messages that carry them hold them.
type observation struct {
	lastSuccessAt *timestamppb.Timestamp
	errorKind     *notificationspb.SourceFailureKind
	errorMessage  *string
	errorSince    *timestamppb.Timestamp
	errorObserved *timestamppb.Timestamp
}

func observe(lastSuccessAt *time.Time, failure *store.SourceFailure) (observation, error) {
	view := observation{lastSuccessAt: timestamp(lastSuccessAt)}
	if failure == nil {
		return view, nil
	}
	kind, err := failureKind(failure.Kind)
	if err != nil {
		return observation{}, err
	}
	view.errorKind, view.errorMessage = &kind, &failure.Message
	view.errorSince, view.errorObserved = timestamppb.New(failure.Since), timestamppb.New(failure.ObservedAt)
	return view, nil
}

var eventNames = []struct {
	view  notificationspb.GitHubEventName
	store store.EventName
}{
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PULL_REQUEST, store.EventPullRequest},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_ISSUE_COMMENT, store.EventIssueComment},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PULL_REQUEST_REVIEW, store.EventPullRequestReview},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PULL_REQUEST_REVIEW_COMMENT, store.EventPullRequestReviewComment},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_CHECK_RUN, store.EventCheckRun},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_CHECK_SUITE, store.EventCheckSuite},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_STATUS, store.EventStatus},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PUSH, store.EventPush},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_CREATE, store.EventCreate},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_DELETE, store.EventDelete},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_WORKFLOW_RUN, store.EventWorkflowRun},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_WORKFLOW_JOB, store.EventWorkflowJob},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_ISSUES, store.EventIssues},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_MERGEABILITY_CHANGED, store.EventMergeabilityChanged},
}

func eventNameView(name store.EventName) (notificationspb.GitHubEventName, error) {
	for _, known := range eventNames {
		if known.store == name {
			return known.view, nil
		}
	}
	return 0, fmt.Errorf("stored GitHub event %q is unknown", name)
}

func subjectView(subject store.GitHubSubject) (*notificationspb.GitHubSubject, error) {
	switch subject := subject.(type) {
	case store.PullRequestSubject:
		number, err := toInt32("pull request number", subject.Number)
		return &notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_PullRequest{
			PullRequest: &notificationspb.PullRequestSubject{Number: number},
		}}, err
	case store.IssueSubject:
		number, err := toInt32("issue number", subject.Number)
		return &notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_Issue{
			Issue: &notificationspb.IssueSubject{Number: number},
		}}, err
	case store.BranchSubject:
		return &notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_Branch{
			Branch: &notificationspb.BranchSubject{Name: subject.Name},
		}}, nil
	case store.CommitSubject:
		return &notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_Commit{
			Commit: &notificationspb.CommitSubject{Sha: subject.SHA},
		}}, nil
	default:
		return nil, fmt.Errorf("stored GitHub subject %T is unknown", subject)
	}
}

// subjectKeyView is the subject a shared row is indexed under, which is what subjectKey makes of it.
func subjectKeyView(key store.GitHubSubjectKey) (*notificationspb.GitHubSubject, error) {
	switch key.Kind {
	case "pull_request", "issue":
		number, err := strconv.ParseInt(key.Key, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("stored %s subject key %q is not a number", key.Kind, key.Key)
		}
		if key.Kind == "issue" {
			return subjectView(store.IssueSubject{Number: number})
		}
		return subjectView(store.PullRequestSubject{Number: number})
	case "branch":
		return subjectView(store.BranchSubject{Name: key.Key})
	case "commit":
		return subjectView(store.CommitSubject{SHA: key.Key})
	default:
		return nil, fmt.Errorf("stored GitHub subject kind %q is unknown", key.Kind)
	}
}

func sourceView(source store.Source) (*notificationspb.Source, error) {
	switch source := source.(type) {
	case store.ActionsSource:
		afterSequence, err := toInt32("after_sequence", source.AfterSequence)
		return &notificationspb.Source{Provider: &notificationspb.Source_Actions{Actions: &notificationspb.ActionsSource{
			RequestId: source.RequestID.String(), AfterSequence: afterSequence,
		}}}, err
	case store.GitHubSource:
		subject, err := subjectView(source.Subject)
		if err != nil {
			return nil, err
		}
		github := &notificationspb.GitHubSource{Repository: source.Repository, Subject: subject}
		for _, filter := range source.Events {
			event, err := eventNameView(filter.Event)
			if err != nil {
				return nil, err
			}
			github.Events = append(github.Events, &notificationspb.GitHubEventFilter{Event: event, Actions: filter.Actions})
		}
		return &notificationspb.Source{Provider: &notificationspb.Source_Github{Github: github}}, nil
	default:
		return nil, fmt.Errorf("stored source %T is unknown", source)
	}
}

func refreshStatus(refresh store.GitHubRefreshStatus) (observation, error) {
	return observe(refresh.LastSuccessAt, refresh.Failure)
}

func githubStatusView(status *store.GitHubStatus) (*notificationspb.GitHubStatus, error) {
	if status == nil {
		return nil, nil
	}
	view := &notificationspb.GitHubStatus{}
	if status.CheckRuns != nil {
		seen, err := refreshStatus(*status.CheckRuns)
		if err != nil {
			return nil, err
		}
		view.CheckRuns = &notificationspb.GitHubRefreshStatus{
			LastSuccessAt: seen.lastSuccessAt, ErrorKind: seen.errorKind, Error: seen.errorMessage,
			ErrorSince: seen.errorSince, ErrorObservedAt: seen.errorObserved,
			RetryAt: timestamp(status.CheckRuns.RetryAt), RefreshingUntil: timestamp(status.CheckRuns.RefreshingUntil),
		}
	}
	if status.CommitStatuses != nil {
		seen, err := refreshStatus(*status.CommitStatuses)
		if err != nil {
			return nil, err
		}
		view.CommitStatuses = &notificationspb.GitHubRefreshStatus{
			LastSuccessAt: seen.lastSuccessAt, ErrorKind: seen.errorKind, Error: seen.errorMessage,
			ErrorSince: seen.errorSince, ErrorObservedAt: seen.errorObserved,
			RetryAt: timestamp(status.CommitStatuses.RetryAt), RefreshingUntil: timestamp(status.CommitStatuses.RefreshingUntil),
		}
	}
	if status.Reviews != nil {
		seen, err := refreshStatus(*status.Reviews)
		if err != nil {
			return nil, err
		}
		view.Reviews = &notificationspb.GitHubRefreshStatus{
			LastSuccessAt: seen.lastSuccessAt, ErrorKind: seen.errorKind, Error: seen.errorMessage,
			ErrorSince: seen.errorSince, ErrorObservedAt: seen.errorObserved,
			RetryAt: timestamp(status.Reviews.RetryAt), RefreshingUntil: timestamp(status.Reviews.RefreshingUntil),
		}
	}
	for _, access := range status.Access {
		seen, err := refreshStatus(access.GitHubRefreshStatus)
		if err != nil {
			return nil, err
		}
		view.Access = append(view.Access, &notificationspb.GitHubAccessStatus{
			AppId: access.Key.AppID, InstallationId: access.Key.InstallationID, RepositoryId: access.Key.RepositoryID,
			CheckedAt: timestamp(access.CheckedAt), ValidUntil: timestamp(access.ValidUntil), CurrentlyValid: access.CurrentlyValid,
			LastSuccessAt: seen.lastSuccessAt, ErrorKind: seen.errorKind, Error: seen.errorMessage,
			ErrorSince: seen.errorSince, ErrorObservedAt: seen.errorObserved,
			RetryAt: timestamp(access.RetryAt), RefreshingUntil: timestamp(access.RefreshingUntil),
		})
	}
	seen, err := refreshStatus(status.Subject.GitHubRefreshStatus)
	if err != nil {
		return nil, err
	}
	subject, err := subjectKeyView(status.Subject.Key)
	if err != nil {
		return nil, err
	}
	view.Subject = &notificationspb.GitHubSubjectStatus{
		RepositoryId: status.Subject.Key.RepositoryID, Subject: subject,
		LastSuccessAt: seen.lastSuccessAt, ErrorKind: seen.errorKind, Error: seen.errorMessage,
		ErrorSince: seen.errorSince, ErrorObservedAt: seen.errorObserved,
		RetryAt: timestamp(status.Subject.RetryAt), RefreshingUntil: timestamp(status.Subject.RefreshingUntil),
	}
	return view, nil
}

func subscriptionView(subscription store.Subscription) (*notificationspb.Subscription, error) {
	source, err := sourceView(subscription.Source)
	if err != nil {
		return nil, err
	}
	seen, err := observe(subscription.LastSuccessAt, subscription.Failure)
	if err != nil {
		return nil, err
	}
	github, err := githubStatusView(subscription.GitHub)
	if err != nil {
		return nil, err
	}
	return &notificationspb.Subscription{
		Id: subscription.ID.String(), InboxId: subscription.InboxID.String(), Source: source,
		IdempotencyKey: subscription.IdempotencyKey, Version: subscription.Version, Cancelled: subscription.Cancelled,
		ExpiresAt: timestamppb.New(subscription.ExpiresAt), LastSuccessAt: seen.lastSuccessAt,
		ErrorKind: seen.errorKind, Error: seen.errorMessage, ErrorSince: seen.errorSince, ErrorObservedAt: seen.errorObserved,
		RetryAt: timestamp(subscription.RetryAt), Github: github,
	}, nil
}

func destinationRefView(ref store.DestinationRef) *notificationspb.DestinationRef {
	return &notificationspb.DestinationRef{Namespace: ref.Namespace, Name: ref.Name, Uid: ref.UID}
}

func inboxView(inbox store.Inbox) *notificationspb.Inbox {
	return &notificationspb.Inbox{
		Id: inbox.ID.String(), DestinationRef: destinationRefView(inbox.DestinationRef), SessionId: inbox.SessionID,
		LastCursor: inbox.LastCursor, Acknowledged: inbox.Acknowledged, Covered: inbox.Covered,
		ExpiredThrough: inbox.ExpiredThrough, Retired: inbox.Retired, DeliveryError: inbox.DeliveryError,
	}
}

func noticeView(notice *store.Notice) *notificationspb.Notice {
	if notice == nil {
		return nil
	}
	return &notificationspb.Notice{
		CommandId: notice.CommandID.String(), ThroughCursor: notice.ThroughCursor,
		Attempted: notice.Attempted, Admitted: notice.Admitted, Confirmed: notice.Confirmed, Error: notice.Error,
	}
}

func eventIdentityView(event store.EventIdentity) (*notificationspb.EventIdentity, error) {
	switch event := event.(type) {
	case store.ActionsEvent:
		sequence, err := toInt32("event sequence", event.Sequence)
		return &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_Actions{Actions: &notificationspb.ActionsEvent{
			RequestId: event.RequestID.String(), Sequence: sequence,
		}}}, err
	case store.GitHubEvent:
		name, err := eventNameView(event.Event)
		if err != nil {
			return nil, err
		}
		return &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_Github{Github: &notificationspb.GitHubEvent{
			AppId: event.AppID, DeliveryId: event.DeliveryID.String(), RepositoryId: event.RepositoryID,
			Event: name, Action: event.Action,
		}}}, nil
	case store.GitHubObservationEvent:
		if event.Event != store.EventCheckRun && event.Event != store.EventStatus && event.Event != store.EventPullRequestReview && event.Event != store.EventPullRequest {
			return nil, fmt.Errorf("stored GitHub observation event %q is unknown", event.Event)
		}
		name, err := eventNameView(event.Event)
		if err != nil {
			return nil, err
		}
		return &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubObservation{
			GithubObservation: &notificationspb.GitHubObservationEvent{
				AppId: event.AppID, RepositoryId: event.RepositoryID, Event: name, Sequence: event.Sequence,
			},
		}}, nil
	case store.GitHubMergeabilityEvent:
		return &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubMergeability{
			GithubMergeability: &notificationspb.GitHubMergeabilityEvent{
				AppId: event.AppID, RepositoryId: event.RepositoryID,
				PullRequestNumber: event.PullRequestNumber,
				Observation:       &notificationspb.GitHubMergeabilityEvent_Sequence{Sequence: event.Sequence},
			},
		}}, nil
	case store.GitHubMergeabilitySnapshotEvent:
		return &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubMergeability{
			GithubMergeability: &notificationspb.GitHubMergeabilityEvent{
				AppId: event.AppID, RepositoryId: event.RepositoryID,
				PullRequestNumber: event.PullRequestNumber,
				Observation:       &notificationspb.GitHubMergeabilityEvent_SnapshotVersion{SnapshotVersion: event.Version},
			},
		}}, nil
	case store.MessageEvent:
		return &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_Message{Message: &notificationspb.MessageEvent{
			MessageId: event.MessageID.String(),
			Sender:    &notificationspb.ServiceAccount{Namespace: event.Sender.Namespace, Name: event.Sender.Name},
		}}}, nil
	default:
		return nil, fmt.Errorf("stored event identity %T is unknown", event)
	}
}

func entryView(entry store.Entry) (*notificationspb.InboxEntry, error) {
	event, err := eventIdentityView(entry.Event)
	if err != nil {
		return nil, err
	}
	payload := &structpb.Struct{}
	if err := protojson.Unmarshal(entry.Payload, payload); err != nil {
		return nil, fmt.Errorf("stored payload of entry %d is not a JSON object: %w", entry.Cursor, err)
	}
	view := &notificationspb.InboxEntry{Cursor: entry.Cursor, Event: event, Payload: payload}
	for _, id := range entry.SubscriptionIDs {
		view.SubscriptionIds = append(view.SubscriptionIds, id.String())
	}
	return view, nil
}
