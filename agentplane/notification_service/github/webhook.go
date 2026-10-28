package github

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/google/go-github/v88/github"
	"github.com/google/uuid"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
)

var (
	// ErrUnsupportedEvent is a webhook event this service takes no deliveries of.
	ErrUnsupportedEvent = errors.New("unsupported GitHub webhook event")
	// ErrMissingRepository is a delivery of an event about a repository that names none.
	ErrMissingRepository = errors.New("repository event requires a repository")
	// ErrInvalidPayload is a body that is not the JSON object GitHub sends for the event.
	ErrInvalidPayload = errors.New("invalid GitHub webhook payload")
)

func invalidPayload(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalidPayload, fmt.Sprintf(format, args...))
}

// object is a JSON object. Its keys match exactly, which encoding/json's struct decoding does not.
type object map[string]json.RawMessage

func asObject(raw json.RawMessage, path string) (object, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil || fields == nil {
		return nil, invalidPayload("%s is not an object", path)
	}
	return fields, nil
}

// present is the field's value, nil if the field is absent or null.
func (o object) present(name string) json.RawMessage {
	raw := o[name]
	if string(raw) == "null" {
		return nil
	}
	return raw
}

func (o object) required(name, path string) (json.RawMessage, error) {
	raw := o.present(name)
	if raw == nil {
		return nil, invalidPayload("%s.%s is required", path, name)
	}
	return raw, nil
}

func (o object) object(name, path string) (object, error) {
	raw, err := o.required(name, path)
	if err != nil {
		return nil, err
	}
	return asObject(raw, path+"."+name)
}

// optionalObject is nil if the field is absent or null.
func (o object) optionalObject(name, path string) (object, error) {
	raw := o.present(name)
	if raw == nil {
		return nil, nil
	}
	return asObject(raw, path+"."+name)
}

func (o object) text(name, path string) (string, error) {
	raw, err := o.required(name, path)
	if err != nil {
		return "", err
	}
	return asText(raw, path+"."+name)
}

// optionalText is nil if the field is absent or null.
func (o object) optionalText(name, path string) (*string, error) {
	raw := o.present(name)
	if raw == nil {
		return nil, nil
	}
	text, err := asText(raw, path+"."+name)
	return &text, err
}

func asText(raw json.RawMessage, path string) (string, error) {
	var text string
	if len(raw) == 0 || raw[0] != '"' || json.Unmarshal(raw, &text) != nil {
		return "", invalidPayload("%s is not a string", path)
	}
	return text, nil
}

// integer is a JSON integer. GitHub sends nothing else for an ID or a number.
func (o object) integer(name, path string) (int64, error) {
	raw, err := o.required(name, path)
	if err != nil {
		return 0, err
	}
	value, err := strconv.ParseInt(string(raw), 10, 64)
	if err != nil {
		return 0, invalidPayload("%s.%s is not an integer", path, name)
	}
	return value, nil
}

func (o object) positive(name, path string) (int64, error) {
	value, err := o.integer(name, path)
	if err == nil && value <= 0 {
		err = invalidPayload("%s.%s must be positive", path, name)
	}
	return value, err
}

// list is the objects of an array field. An absent field is an empty list, a null one is invalid.
func (o object) list(name, path string) ([]object, error) {
	raw, ok := o[name]
	if !ok {
		return nil, nil
	}
	var elements []json.RawMessage
	if err := json.Unmarshal(raw, &elements); err != nil || elements == nil {
		return nil, invalidPayload("%s.%s is not a list", path, name)
	}
	objects := make([]object, len(elements))
	for i, element := range elements {
		var err error
		if objects[i], err = asObject(element, fmt.Sprintf("%s.%s[%d]", path, name, i)); err != nil {
			return nil, err
		}
	}
	return objects, nil
}

func parseRepository(o object, path string) (*store.GitHubRepository, error) {
	if o == nil {
		return nil, nil
	}
	id, err := o.positive("id", path)
	if err != nil {
		return nil, err
	}
	name, err := o.text("full_name", path)
	if err != nil {
		return nil, err
	}
	if len(name) > maxRepositoryNameLength || !repositoryName.MatchString(name) {
		return nil, invalidPayload("%s.full_name is not owner/name", path)
	}
	return &store.GitHubRepository{ID: id, Name: name}, nil
}

// envelope is what every delivery carries.
type envelope struct {
	installationID int64
	repository     *store.GitHubRepository
	action         *string
}

func parseEnvelope(body object) (envelope, error) {
	var parsed envelope
	installation, err := body.object("installation", "body")
	if err != nil {
		return envelope{}, err
	}
	if parsed.installationID, err = installation.positive("id", "installation"); err != nil {
		return envelope{}, err
	}
	if suspended, err := installation.optionalText("suspended_at", "installation"); err != nil {
		return envelope{}, err
	} else if suspended != nil {
		if _, err := time.Parse(time.RFC3339, *suspended); err != nil {
			return envelope{}, invalidPayload("installation.suspended_at is not a timestamp")
		}
	}
	repository, err := body.optionalObject("repository", "body")
	if err != nil {
		return envelope{}, err
	}
	if parsed.repository, err = parseRepository(repository, "repository"); err != nil {
		return envelope{}, err
	}
	if parsed.action, err = body.optionalText("action", "body"); err != nil {
		return envelope{}, err
	}
	if parsed.action != nil && utf8.RuneCountInString(*parsed.action) > 64 {
		return envelope{}, invalidPayload("body.action is longer than 64 characters")
	}
	return parsed, nil
}

// correlation is what a delivery says about the subjects it concerns, before they are checked.
type correlation struct {
	// sha is the commit the event is about.
	sha      *string
	subjects []store.GitHubSubject
	// head is where a pull request's head commit lives, which for a fork is not the repository of the event.
	head *store.GitHubRepository
}

// A correlator reads the part of a body that its event adds to the envelope, which GitHub names in a header, not
// in the body.
type correlator func(body object) (correlation, error)

// correlators are the events this service takes, with how each is read. The installation events carry the
// envelope alone.
var correlators = map[string]correlator{
	string(store.EventPullRequest):              pullRequestEvent,
	string(store.EventPullRequestReview):        pullRequestEvent,
	string(store.EventPullRequestReviewComment): pullRequestEvent,
	string(store.EventIssueComment):             issueEvent,
	string(store.EventIssues):                   issueEvent,
	string(store.EventCheckRun):                 checkEvent("check_run"),
	string(store.EventCheckSuite):               checkEvent("check_suite"),
	string(store.EventWorkflowRun):              workflowRunEvent,
	string(store.EventWorkflowJob):              workflowJobEvent,
	string(store.EventStatus):                   statusEvent,
	string(store.EventPush):                     pushEvent,
	string(store.EventCreate):                   refEvent,
	string(store.EventDelete):                   refEvent,
	"installation":                              func(object) (correlation, error) { return correlation{}, nil },
	"installation_repositories":                 func(object) (correlation, error) { return correlation{}, nil },
}

func pullRequestEvent(body object) (correlation, error) {
	pullRequest, err := body.object("pull_request", "body")
	if err != nil {
		return correlation{}, err
	}
	number, err := pullRequest.integer("number", "pull_request")
	if err != nil {
		return correlation{}, err
	}
	head, err := pullRequest.object("head", "pull_request")
	if err != nil {
		return correlation{}, err
	}
	sha, err := head.text("sha", "pull_request.head")
	if err != nil {
		return correlation{}, err
	}
	repository, err := head.optionalObject("repo", "pull_request.head")
	if err != nil {
		return correlation{}, err
	}
	headRepository, err := parseRepository(repository, "pull_request.head.repo")
	if err != nil {
		return correlation{}, err
	}
	return correlation{sha: &sha, subjects: []store.GitHubSubject{store.PullRequestSubject{Number: number}}, head: headRepository}, nil
}

func issueEvent(body object) (correlation, error) {
	issue, err := body.object("issue", "body")
	if err != nil {
		return correlation{}, err
	}
	number, err := issue.integer("number", "issue")
	if err != nil {
		return correlation{}, err
	}
	// Any object marks a pull request, an empty one too.
	pullRequest, err := issue.optionalObject("pull_request", "issue")
	if err != nil {
		return correlation{}, err
	}
	if pullRequest != nil {
		return correlation{subjects: []store.GitHubSubject{store.PullRequestSubject{Number: number}}}, nil
	}
	return correlation{subjects: []store.GitHubSubject{store.IssueSubject{Number: number}}}, nil
}

// pullRequests are the pull requests a check or workflow run lists.
func pullRequests(check object, path string) ([]store.GitHubSubject, error) {
	listed, err := check.list("pull_requests", path)
	if err != nil {
		return nil, err
	}
	subjects := make([]store.GitHubSubject, len(listed))
	for i, pullRequest := range listed {
		number, err := pullRequest.integer("number", fmt.Sprintf("%s.pull_requests[%d]", path, i))
		if err != nil {
			return nil, err
		}
		subjects[i] = store.PullRequestSubject{Number: number}
	}
	return subjects, nil
}

func checkEvent(key string) correlator {
	return func(body object) (correlation, error) {
		check, err := body.object(key, "body")
		if err != nil {
			return correlation{}, err
		}
		sha, err := check.text("head_sha", key)
		if err != nil {
			return correlation{}, err
		}
		subjects, err := pullRequests(check, key)
		return correlation{sha: &sha, subjects: subjects}, err
	}
}

func workflowRunEvent(body object) (correlation, error) {
	run, err := body.object("workflow_run", "body")
	if err != nil {
		return correlation{}, err
	}
	sha, err := run.text("head_sha", "workflow_run")
	if err != nil {
		return correlation{}, err
	}
	subjects, err := pullRequests(run, "workflow_run")
	if err != nil {
		return correlation{}, err
	}
	branch, err := run.optionalText("head_branch", "workflow_run")
	if err != nil {
		return correlation{}, err
	}
	if branch != nil {
		subjects = append(subjects, store.BranchSubject{Name: *branch})
	}
	return correlation{sha: &sha, subjects: subjects}, nil
}

func workflowJobEvent(body object) (correlation, error) {
	job, err := body.object("workflow_job", "body")
	if err != nil {
		return correlation{}, err
	}
	sha, err := job.text("head_sha", "workflow_job")
	if err != nil {
		return correlation{}, err
	}
	branch, err := job.optionalText("head_branch", "workflow_job")
	if err != nil {
		return correlation{}, err
	}
	found := correlation{sha: &sha}
	if branch != nil {
		found.subjects = []store.GitHubSubject{store.BranchSubject{Name: *branch}}
	}
	return found, nil
}

func statusEvent(body object) (correlation, error) {
	sha, err := body.text("sha", "body")
	if err != nil {
		return correlation{}, err
	}
	branches, err := body.list("branches", "body")
	if err != nil {
		return correlation{}, err
	}
	found := correlation{sha: &sha}
	for i, branch := range branches {
		name, err := branch.text("name", fmt.Sprintf("branches[%d]", i))
		if err != nil {
			return correlation{}, err
		}
		found.subjects = append(found.subjects, store.BranchSubject{Name: name})
	}
	return found, nil
}

const (
	branchRefPrefix = "refs/heads/"
	// zeroSHA is the commit a push to delete a branch leaves it at.
	zeroSHA = "0000000000000000000000000000000000000000"
)

func pushEvent(body object) (correlation, error) {
	ref, err := body.text("ref", "body")
	if err != nil {
		return correlation{}, err
	}
	after, err := body.text("after", "body")
	if err != nil {
		return correlation{}, err
	}
	name, isBranch := strings.CutPrefix(ref, branchRefPrefix)
	if !isBranch {
		return correlation{}, nil
	}
	found := correlation{subjects: []store.GitHubSubject{store.BranchSubject{Name: name}}}
	if after != zeroSHA {
		found.sha = &after
	}
	return found, nil
}

func refEvent(body object) (correlation, error) {
	ref, err := body.text("ref", "body")
	if err != nil {
		return correlation{}, err
	}
	refType, err := body.text("ref_type", "body")
	if err != nil {
		return correlation{}, err
	}
	switch refType {
	case "branch":
		return correlation{subjects: []store.GitHubSubject{store.BranchSubject{Name: ref}}}, nil
	case "tag":
		return correlation{}, nil
	default:
		return correlation{}, invalidPayload("body.ref_type is not branch or tag")
	}
}

// ParseDelivery reads a webhook delivery, whose signature the caller has checked, into what is stored for it:
// the commit it is about, the subjects it concerns and the revisions those are seen at. It fails with
// ErrUnsupportedEvent, ErrMissingRepository or ErrInvalidPayload.
//
// A body is a JSON object, and the fields read have exact types and names: an ID or a number must be a JSON
// integer, and a timestamp RFC 3339.
func ParseDelivery(appID int64, event string, deliveryID uuid.UUID, raw []byte) (store.GitHubIngest, error) {
	correlate, ok := correlators[event]
	if !ok {
		return store.GitHubIngest{}, ErrUnsupportedEvent
	}
	if !utf8.Valid(raw) || !json.Valid(raw) {
		return store.GitHubIngest{}, invalidPayload("body is not JSON")
	}
	body, err := asObject(raw, "body")
	if err != nil {
		return store.GitHubIngest{}, err
	}
	common, err := parseEnvelope(body)
	if err != nil {
		return store.GitHubIngest{}, err
	}
	found, err := correlate(body)
	if err != nil {
		return store.GitHubIngest{}, err
	}
	if common.repository == nil && event != "installation" && event != "installation_repositories" {
		return store.GitHubIngest{}, ErrMissingRepository
	}
	ingest := store.GitHubIngest{
		AppID:          appID,
		DeliveryID:     deliveryID,
		InstallationID: common.installationID,
		Repository:     common.repository,
		Event:          event,
		Payload:        raw,
		Action:         common.action,
		HeadSHA:        found.sha,
	}
	digest := sha256.New()
	digest.Write([]byte(event))
	digest.Write([]byte{0})
	digest.Write(raw)
	ingest.Digest = digest.Sum(nil)
	for _, subject := range found.subjects {
		key, err := store.NewGitHubSubjectKey(common.repository.ID, subject)
		if err != nil {
			return store.GitHubIngest{}, fmt.Errorf("%w: %w", ErrInvalidPayload, err)
		}
		ingest.Subjects = append(ingest.Subjects, key)
	}
	if common.repository != nil && found.sha != nil {
		ingest.Revisions = append(ingest.Revisions, store.GitHubHeadRevision{Repository: *common.repository, SHA: *found.sha})
		if found.head != nil {
			ingest.Revisions = append(ingest.Revisions, store.GitHubHeadRevision{Repository: *found.head, SHA: *found.sha})
		}
	}
	if common.repository != nil && found.sha != nil {
		switch event {
		case string(store.EventCheckRun):
			checkRun, err := webhookCheckRun(body, common.action, *found.sha)
			if err != nil {
				return store.GitHubIngest{}, fmt.Errorf("%w: %w", ErrInvalidPayload, err)
			}
			if checkRun != nil {
				checkRun.RepositoryID = common.repository.ID
				ingest.CheckRun = checkRun
			}
		case string(store.EventStatus):
			commitStatus, err := webhookCommitStatus(body, *found.sha)
			if err != nil {
				return store.GitHubIngest{}, fmt.Errorf("%w: %w", ErrInvalidPayload, err)
			}
			if commitStatus != nil {
				commitStatus.RepositoryID = common.repository.ID
				ingest.CommitStatus = commitStatus
			}
		}
	}
	return ingest, nil
}

// A check webhook may carry only correlation fields. Preserve those as raw activity; complete state
// observations share normalization with polling so the same change has one durable identity.
func webhookCheckRun(body object, action *string, sha string) (*store.GitHubCheckRun, error) {
	if action == nil || (*action != "created" && *action != "in_progress" && *action != "completed") {
		return nil, nil
	}
	check, err := body.optionalObject("check_run", "body")
	if err != nil {
		return nil, err
	}
	if check == nil || check.present("id") == nil || check.present("head_sha") == nil ||
		check.present("name") == nil || check.present("status") == nil {
		return nil, nil
	}
	var run github.CheckRun
	if err := json.Unmarshal(body["check_run"], &run); err != nil {
		return nil, err
	}
	observation, err := checkRunObservation(&run, sha)
	if err != nil {
		return nil, err
	}
	// A check may be created already completed. Keep its state independent from the raw webhook action.
	return &observation, nil
}

// A status webhook may carry only correlation fields. Preserve those as raw activity.
func webhookCommitStatus(body object, sha string) (*store.GitHubCommitStatus, error) {
	if body.present("id") == nil || body.present("context") == nil || body.present("state") == nil {
		return nil, nil
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	var status github.RepoStatus
	if err := json.Unmarshal(raw, &status); err != nil {
		return nil, err
	}
	observation, err := statusObservation(&status, sha)
	if err != nil {
		return nil, err
	}
	return &observation, nil
}
