package rpc

import (
	"fmt"
	"math"
	"strconv"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	inboxstore "github.com/agentydragon/ducktape/agentplane/notification_service/internal/inbox"
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// The views below turn what the inbox reads into the messages the APIs answer with. A row the inbox holds that no
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

func failureKind(kind model.SourceFailureKind) (notificationspb.SourceFailureKind, error) {
	switch kind {
	case model.FailureRateLimited:
		return notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_RATE_LIMITED, nil
	case model.FailureUnavailable:
		return notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_UNAVAILABLE, nil
	case model.FailureAccessDenied:
		return notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_ACCESS_DENIED, nil
	case model.FailureSourceChanged:
		return notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_SOURCE_CHANGED, nil
	case model.FailureProcessingError:
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

func observe(lastSuccessAt *time.Time, failure *model.SourceFailure) (observation, error) {
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
	store githubmodel.EventName
}{
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PULL_REQUEST, githubmodel.EventPullRequest},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_ISSUE_COMMENT, githubmodel.EventIssueComment},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PULL_REQUEST_REVIEW, githubmodel.EventPullRequestReview},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PULL_REQUEST_REVIEW_COMMENT, githubmodel.EventPullRequestReviewComment},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_CHECK_RUN, githubmodel.EventCheckRun},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_CHECK_SUITE, githubmodel.EventCheckSuite},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_STATUS, githubmodel.EventStatus},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PUSH, githubmodel.EventPush},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_CREATE, githubmodel.EventCreate},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_DELETE, githubmodel.EventDelete},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_WORKFLOW_RUN, githubmodel.EventWorkflowRun},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_WORKFLOW_JOB, githubmodel.EventWorkflowJob},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_ISSUES, githubmodel.EventIssues},
	{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_MERGEABILITY_CHANGED, githubmodel.EventMergeabilityChanged},
}

func eventNameView(name githubmodel.EventName) (notificationspb.GitHubEventName, error) {
	for _, known := range eventNames {
		if known.store == name {
			return known.view, nil
		}
	}
	return 0, fmt.Errorf("stored GitHub event %q is unknown", name)
}

func subjectView(subject githubmodel.Subject) (*notificationspb.GitHubSubject, error) {
	switch subject := subject.(type) {
	case githubmodel.PullRequestSubject:
		number, err := toInt32("pull request number", subject.Number)
		return &notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_PullRequest{
			PullRequest: &notificationspb.PullRequestSubject{Number: number},
		}}, err
	case githubmodel.IssueSubject:
		number, err := toInt32("issue number", subject.Number)
		return &notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_Issue{
			Issue: &notificationspb.IssueSubject{Number: number},
		}}, err
	case githubmodel.BranchSubject:
		return &notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_Branch{
			Branch: &notificationspb.BranchSubject{Name: subject.Name},
		}}, nil
	case githubmodel.CommitSubject:
		return &notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_Commit{
			Commit: &notificationspb.CommitSubject{Sha: subject.SHA},
		}}, nil
	default:
		return nil, fmt.Errorf("stored GitHub subject %T is unknown", subject)
	}
}

// subjectKeyView is the subject a shared row is indexed under, which is what subjectKey makes of it.
func subjectKeyView(key githubstate.SubjectKey) (*notificationspb.GitHubSubject, error) {
	switch key.Kind {
	case "pull_request", "issue":
		number, err := strconv.ParseInt(key.Key, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("stored %s subject key %q is not a number", key.Kind, key.Key)
		}
		if key.Kind == "issue" {
			return subjectView(githubmodel.IssueSubject{Number: number})
		}
		return subjectView(githubmodel.PullRequestSubject{Number: number})
	case "branch":
		return subjectView(githubmodel.BranchSubject{Name: key.Key})
	case "commit":
		return subjectView(githubmodel.CommitSubject{SHA: key.Key})
	default:
		return nil, fmt.Errorf("stored GitHub subject kind %q is unknown", key.Kind)
	}
}

func sourceView(source sourceadapter.Source) (*notificationspb.Source, error) {
	switch source := source.(type) {
	case sourceadapter.ActionsSource:
		afterSequence, err := toInt32("after_sequence", source.AfterSequence)
		return &notificationspb.Source{Provider: &notificationspb.Source_Actions{Actions: &notificationspb.ActionsSource{
			RequestId: source.RequestID.String(), AfterSequence: afterSequence,
		}}}, err
	case sourceadapter.GitHubSource:
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

// refreshStatusView is the message for a shared row's refresh state; nil when the status carries no such row.
func refreshStatusView(refresh *githubstate.RefreshStatus) (*notificationspb.GitHubRefreshStatus, error) {
	if refresh == nil {
		return nil, nil
	}
	seen, err := observe(refresh.LastSuccessAt, refresh.Failure)
	if err != nil {
		return nil, err
	}
	return &notificationspb.GitHubRefreshStatus{
		LastSuccessAt: seen.lastSuccessAt, ErrorKind: seen.errorKind, Error: seen.errorMessage,
		ErrorSince: seen.errorSince, ErrorObservedAt: seen.errorObserved,
		RetryAt: timestamp(refresh.RetryAt), RefreshingUntil: timestamp(refresh.RefreshingUntil),
	}, nil
}

func githubStatusView(status *githubstate.Status) (*notificationspb.GitHubStatus, error) {
	if status == nil {
		return nil, nil
	}
	view := &notificationspb.GitHubStatus{}
	var err error
	if view.CheckRuns, err = refreshStatusView(status.CheckRuns); err != nil {
		return nil, err
	}
	if view.CommitStatuses, err = refreshStatusView(status.CommitStatuses); err != nil {
		return nil, err
	}
	if view.Reviews, err = refreshStatusView(status.Reviews); err != nil {
		return nil, err
	}
	for _, access := range status.Access {
		seen, err := observe(access.LastSuccessAt, access.Failure)
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
	seen, err := observe(status.Subject.LastSuccessAt, status.Subject.Failure)
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

func subscriptionView(subscription inboxstore.Subscription) (*notificationspb.Subscription, error) {
	source, err := sourceView(subscription.Source)
	if err != nil {
		return nil, err
	}
	seen, err := observe(subscription.LastSuccessAt, subscription.Failure)
	if err != nil {
		return nil, err
	}
	var github *notificationspb.GitHubStatus
	switch status := subscription.SourceStatus.(type) {
	case sourceadapter.ActionsStatus:
	case sourceadapter.GitHubStatus:
		github, err = githubStatusView(&status.Status)
		if err != nil {
			return nil, err
		}
	default:
		return nil, fmt.Errorf("subscription %s: unknown source status %T", subscription.ID, status)
	}
	return &notificationspb.Subscription{
		Id: subscription.ID.String(), InboxId: subscription.InboxID.String(), Source: source,
		IdempotencyKey: proto.String(subscription.IdempotencyKey), Version: proto.Int64(subscription.Version), Cancelled: subscription.Cancelled,
		ExpiresAt: timestamppb.New(subscription.ExpiresAt), LastSuccessAt: seen.lastSuccessAt,
		ErrorKind: seen.errorKind, Error: seen.errorMessage, ErrorSince: seen.errorSince, ErrorObservedAt: seen.errorObserved,
		RetryAt: timestamp(subscription.RetryAt), Github: github,
		NextSourceCheckAt: timestamp(subscription.NextSourceCheckAt), Expired: subscription.Expired,
	}, nil
}

func inboxView(inbox inboxstore.Inbox) *notificationspb.Inbox {
	return &notificationspb.Inbox{
		Id: inbox.ID.String(), SessionId: inbox.SessionID,
		LastCursor: inbox.LastCursor, Acknowledged: inbox.Acknowledged, Covered: inbox.Covered,
		ExpiredThrough: inbox.ExpiredThrough, Retired: inbox.Retired, DeliveryError: inbox.DeliveryError,
	}
}

func noticeView(notice *inboxstore.Notice) *notificationspb.Notice {
	if notice == nil {
		return nil
	}
	return &notificationspb.Notice{
		CommandId: notice.CommandID.String(), ThroughCursor: notice.ThroughCursor,
		Attempted: notice.Attempted, Admitted: notice.Admitted, Confirmed: notice.Confirmed, Error: notice.Error,
	}
}

func eventIdentityView(event sourceadapter.EventIdentity) (*notificationspb.EventIdentity, error) {
	switch event := event.(type) {
	case sourceadapter.ActionsEvent:
		sequence, err := toInt32("event sequence", event.Sequence)
		return &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_Actions{Actions: &notificationspb.ActionsEvent{
			RequestId: event.RequestID.String(), Sequence: sequence,
		}}}, err
	case sourceadapter.GitHubEvent:
		name, err := eventNameView(event.Event)
		if err != nil {
			return nil, err
		}
		return &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_Github{Github: &notificationspb.GitHubEvent{
			AppId: event.AppID, DeliveryId: event.DeliveryID.String(), RepositoryId: event.RepositoryID,
			Event: name, Action: event.Action,
		}}}, nil
	case sourceadapter.GitHubObservationEvent:
		if event.Event != githubmodel.EventCheckRun && event.Event != githubmodel.EventStatus && event.Event != githubmodel.EventPullRequestReview && event.Event != githubmodel.EventPullRequest {
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
	case sourceadapter.GitHubMergeabilityEvent:
		return &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubMergeability{
			GithubMergeability: &notificationspb.GitHubMergeabilityEvent{
				AppId: event.AppID, RepositoryId: event.RepositoryID,
				PullRequestNumber: event.PullRequestNumber,
				Observation:       &notificationspb.GitHubMergeabilityEvent_Sequence{Sequence: event.Sequence},
			},
		}}, nil
	case sourceadapter.GitHubMergeabilitySnapshotEvent:
		return &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubMergeability{
			GithubMergeability: &notificationspb.GitHubMergeabilityEvent{
				AppId: event.AppID, RepositoryId: event.RepositoryID,
				PullRequestNumber: event.PullRequestNumber,
				Observation:       &notificationspb.GitHubMergeabilityEvent_SnapshotVersion{SnapshotVersion: event.Version},
			},
		}}, nil
	case sourceadapter.MessageEvent:
		message := &notificationspb.MessageEvent{
			MessageId: event.MessageID.String(),
			Sender:    &identitypb.ServiceAccount{Namespace: event.Sender.Namespace, Name: event.Sender.Name},
		}
		if event.SenderIdentity != nil {
			message.SenderIdentity = &identitypb.Identity{Name: event.SenderIdentity.Name, Uid: event.SenderIdentity.UID}
		}
		return &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_Message{Message: message}}, nil
	default:
		return nil, fmt.Errorf("stored event identity %T is unknown", event)
	}
}

func entryView(entry inboxstore.Entry) (*notificationspb.InboxEntry, error) {
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
