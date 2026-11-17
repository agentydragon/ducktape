package storagecodec

import (
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/storagepb"
)

// EncodeSubscriptionSource converts the GitHub source variant without changing its persisted protobuf shape.
func EncodeSubscriptionSource(source model.GitHubSource) (*storagepb.GitHubSource, error) {
	github := &storagepb.GitHubSource{Repository: source.Repository}
	switch subject := source.Subject.(type) {
	case model.PullRequestSubject:
		github.Subject = &storagepb.GitHubSource_PullRequest{PullRequest: &storagepb.PullRequest{Number: subject.Number}}
	case model.IssueSubject:
		github.Subject = &storagepb.GitHubSource_Issue{Issue: &storagepb.Issue{Number: subject.Number}}
	case model.BranchSubject:
		github.Subject = &storagepb.GitHubSource_Branch{Branch: &storagepb.Branch{Name: subject.Name}}
	case model.CommitSubject:
		github.Subject = &storagepb.GitHubSource_Commit{Commit: &storagepb.Commit{Sha: subject.SHA}}
	default:
		return nil, fmt.Errorf("unsupported GitHub subject %T", source.Subject)
	}
	for _, filter := range model.NormalizeFilters(source.Events) {
		github.Events = append(github.Events, &storagepb.EventFilter{Event: string(filter.Event), Actions: filter.Actions})
	}
	return github, nil
}

// DecodeSubscriptionSource restores the typed GitHub source from its existing persisted protobuf variant.
func DecodeSubscriptionSource(stored *storagepb.GitHubSource) (model.GitHubSource, error) {
	if stored == nil {
		return model.GitHubSource{}, fmt.Errorf("GitHub source is required")
	}
	github := model.GitHubSource{Repository: stored.GetRepository()}
	switch subject := stored.GetSubject().(type) {
	case *storagepb.GitHubSource_PullRequest:
		if subject.PullRequest == nil {
			return model.GitHubSource{}, fmt.Errorf("pull request subject is required")
		}
		github.Subject = model.PullRequestSubject{Number: subject.PullRequest.GetNumber()}
	case *storagepb.GitHubSource_Issue:
		if subject.Issue == nil {
			return model.GitHubSource{}, fmt.Errorf("issue subject is required")
		}
		github.Subject = model.IssueSubject{Number: subject.Issue.GetNumber()}
	case *storagepb.GitHubSource_Branch:
		if subject.Branch == nil {
			return model.GitHubSource{}, fmt.Errorf("branch subject is required")
		}
		github.Subject = model.BranchSubject{Name: subject.Branch.GetName()}
	case *storagepb.GitHubSource_Commit:
		if subject.Commit == nil {
			return model.GitHubSource{}, fmt.Errorf("commit subject is required")
		}
		github.Subject = model.CommitSubject{SHA: subject.Commit.GetSha()}
	default:
		return model.GitHubSource{}, fmt.Errorf("GitHub subject is required")
	}
	if len(stored.GetEvents()) > 0 {
		github.Events = make([]model.EventFilter, len(stored.GetEvents()))
		for i, filter := range stored.GetEvents() {
			github.Events[i] = model.EventFilter{Event: model.EventName(filter.GetEvent()), Actions: filter.GetActions()}
		}
	}
	return github, nil
}
