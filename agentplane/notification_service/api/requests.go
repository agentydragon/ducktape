package api

import (
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/google/uuid"
)

// defaultLifetimeDays is how long a subscription lasts when the request names no lifetime.
const defaultLifetimeDays = 7

// defaultReadLimit is how many entries a read returns when the request names no limit.
const defaultReadLimit = 128

// The functions below read a request into what the store takes. A request that no store type can hold fails with
// model.ErrInvalid, as a request the store refuses does.

func invalidRequest(format string, args ...any) error {
	return fmt.Errorf("%w: %s", model.ErrInvalid, fmt.Sprintf(format, args...))
}

func parseID(field, value string) (uuid.UUID, error) {
	id, err := uuid.Parse(value)
	if err != nil {
		return uuid.Nil, invalidRequest("%s must be a UUID", field)
	}
	return id, nil
}

func parseOptionalID(field string, value *string) (*uuid.UUID, error) {
	if value == nil {
		return nil, nil
	}
	id, err := parseID(field, *value)
	return &id, err
}

func subscribeBody(request *notificationspb.CreateSubscriptionRequest) (model.Subscribe, error) {
	if request.GetDestinationRef() == nil {
		return model.Subscribe{}, invalidRequest("destination_ref is required")
	}
	source, err := sourceBody(request.GetSource())
	if err != nil {
		return model.Subscribe{}, err
	}
	body := model.Subscribe{
		DestinationRef: model.DestinationRef{
			Namespace: request.GetDestinationRef().GetNamespace(), Name: request.GetDestinationRef().GetName(),
			UID: request.GetDestinationRef().GetUid(),
		},
		SessionID: request.GetSessionId(), IdempotencyKey: request.GetIdempotencyKey(), Source: source,
		LifetimeDays: int64(request.GetLifetimeDays()),
	}
	if request.LifetimeDays == nil {
		body.LifetimeDays = defaultLifetimeDays
	}
	return body, nil
}

func sourceBody(source *notificationspb.Source) (model.Source, error) {
	switch provider := source.GetProvider().(type) {
	case *notificationspb.Source_Actions:
		requestID, err := parseID("source.actions.request_id", provider.Actions.GetRequestId())
		if err != nil {
			return nil, err
		}
		return model.ActionsSource{RequestID: requestID, AfterSequence: int64(provider.Actions.GetAfterSequence())}, nil
	case *notificationspb.Source_Github:
		subject, err := subjectBody(provider.Github.GetSubject())
		if err != nil {
			return nil, err
		}
		github := model.GitHubSource{Repository: provider.Github.GetRepository(), Subject: subject}
		for _, filter := range provider.Github.GetEvents() {
			name, err := eventNameBody(filter.GetEvent())
			if err != nil {
				return nil, err
			}
			github.Events = append(github.Events, model.EventFilter{Event: name, Actions: filter.GetActions()})
		}
		return github, nil
	default:
		return nil, invalidRequest("source must be an Action request or a GitHub subject")
	}
}

func subjectBody(subject *notificationspb.GitHubSubject) (model.GitHubSubject, error) {
	switch kind := subject.GetSubject().(type) {
	case *notificationspb.GitHubSubject_PullRequest:
		return model.PullRequestSubject{Number: int64(kind.PullRequest.GetNumber())}, nil
	case *notificationspb.GitHubSubject_Issue:
		return model.IssueSubject{Number: int64(kind.Issue.GetNumber())}, nil
	case *notificationspb.GitHubSubject_Branch:
		return model.BranchSubject{Name: kind.Branch.GetName()}, nil
	case *notificationspb.GitHubSubject_Commit:
		return model.CommitSubject{SHA: kind.Commit.GetSha()}, nil
	default:
		return nil, invalidRequest("subject must be a pull request, issue, branch or commit")
	}
}

func eventNameBody(name notificationspb.GitHubEventName) (model.EventName, error) {
	for _, known := range eventNames {
		if known.view == name {
			return known.store, nil
		}
	}
	return "", invalidRequest("event %s is not a GitHub event this service accepts", name)
}

// lifetimeDays is the lifetime a renewal asks for.
func lifetimeDays(requested *int32) int64 {
	if requested == nil {
		return defaultLifetimeDays
	}
	return int64(*requested)
}

// readLimit is how many entries a read asks for.
func readLimit(requested *int32) int {
	if requested == nil {
		return defaultReadLimit
	}
	return int(*requested)
}

// errNoSession is a Session the runner does not hold, which is as unknown to the caller as a missing inbox.
var errNoSession = errors.New("the Sandbox's runner holds no such Session")
