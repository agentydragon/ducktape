package store

import (
	"bytes"
	"cmp"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"unicode/utf8"

	"github.com/google/uuid"
)

// ServiceAccount is comparable so ownership checks use namespace/name values, not protobuf pointer identity.
type ServiceAccount struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

// DestinationRef is the Sandbox a Session runs in, by the UID that tells it from a Sandbox recreated under
// the same name. It is stored in the inbox's normalized columns and in a subscription's creation record.
type DestinationRef struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	UID       string `json:"uid"`
}

// Subscribe is a subscription as its creation records it: `subscription.creation`, which an identical retry
// is compared with.
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

// EventIdentity names an entry's event within its source, which is how a redelivery is told from a new event:
// an ActionsEvent, a GitHubEvent, a GitHubMergeabilityEvent, a GitHubMergeabilitySnapshotEvent or a MessageEvent.
type EventIdentity interface{ isEventIdentity() }

type ActionsEvent struct {
	RequestID uuid.UUID
	Sequence  int64
}

type GitHubEvent struct {
	AppID        int64
	DeliveryID   uuid.UUID
	RepositoryID int64
	Event        EventName
	Action       *string
}

// GitHubMergeabilityEvent is one Agentplane-generated observation of a pull request's mergeability.
// Sequence is a durable, globally increasing generated-event sequence, not a GitHub delivery ID.
type GitHubMergeabilityEvent struct {
	AppID             int64
	RepositoryID      int64
	PullRequestNumber int64
	Sequence          int64
}

// GitHubMergeabilitySnapshotEvent identifies a current conflicted snapshot sent to a joining subscriber. Version
// changes when the snapshot's relevant state changes.
type GitHubMergeabilitySnapshotEvent struct {
	AppID             int64
	RepositoryID      int64
	PullRequestNumber int64
	Version           int64
}

// MessageEvent is a message a workload sent to the inbox, with the sender the service authenticated.
type MessageEvent struct {
	MessageID uuid.UUID
	Sender    ServiceAccount
}

func (ActionsEvent) isEventIdentity()                    {}
func (GitHubEvent) isEventIdentity()                     {}
func (GitHubMergeabilityEvent) isEventIdentity()         {}
func (GitHubMergeabilitySnapshotEvent) isEventIdentity() {}
func (MessageEvent) isEventIdentity()                    {}

var (
	repositoryPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
	actionPattern     = regexp.MustCompile(`^[a-z_]+$`)
	shaPattern        = regexp.MustCompile(`^[0-9a-f]{40}$`)
)

// ErrInvalid marks a request that breaks a documented bound or pattern.
var ErrInvalid = errors.New("invalid argument")

func invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalid, fmt.Sprintf(format, args...))
}

// checkText checks a string's length in characters, as the stored model counts it.
func checkText(field, value string, minLength, maxLength int) error {
	if !utf8.ValidString(value) {
		return invalid("%s is not valid UTF-8", field)
	}
	if length := utf8.RuneCountInString(value); length < minLength || length > maxLength {
		return invalid("%s must be %d to %d characters", field, minLength, maxLength)
	}
	return nil
}

func (r DestinationRef) validate() error {
	return errors.Join(
		checkText("destination_ref.namespace", r.Namespace, 1, 63),
		checkText("destination_ref.name", r.Name, 1, 63),
		checkText("destination_ref.uid", r.UID, 1, 128),
	)
}

// Validate checks the bounds and patterns of the creation.
func (s Subscribe) Validate() error {
	if err := errors.Join(
		s.DestinationRef.validate(),
		checkText("session_id", s.SessionID, 1, 200),
		checkText("idempotency_key", s.IdempotencyKey, 1, 200),
	); err != nil {
		return err
	}
	if s.LifetimeDays < 1 || s.LifetimeDays > 30 {
		return invalid("lifetime_days must be 1 to 30")
	}
	switch source := s.Source.(type) {
	case ActionsSource:
		if source.AfterSequence < 0 || source.AfterSequence > 1<<31-1 {
			return invalid("after_sequence must be 0 to 2147483647")
		}
		return nil
	case GitHubSource:
		return source.validate()
	default:
		return invalid("source must be an Action request or a GitHub subject")
	}
}

func (s GitHubSource) validate() error {
	if err := checkText("repository", s.Repository, 1, 200); err != nil {
		return err
	}
	if !repositoryPattern.MatchString(s.Repository) {
		return invalid("repository must be owner/name")
	}
	allowed := slices.Clone(ciEvents)
	switch subject := s.Subject.(type) {
	case PullRequestSubject:
		if subject.Number <= 0 {
			return invalid("pull request number must be positive")
		}
		allowed = append(allowed, prEvents...)
	case IssueSubject:
		if subject.Number <= 0 {
			return invalid("issue number must be positive")
		}
		allowed = issueEvents
	case BranchSubject:
		if err := checkText("branch name", subject.Name, 1, 255); err != nil {
			return err
		}
		allowed = append(allowed, refEvents...)
	case CommitSubject:
		if !shaPattern.MatchString(subject.SHA) {
			return invalid("commit SHA must be 40 lowercase hexadecimal characters")
		}
	default:
		return invalid("subject must be a pull request, issue, branch or commit")
	}
	if s.Events == nil {
		return nil
	}
	for _, filter := range s.Events {
		if filter.Actions != nil && len(filter.Actions) == 0 {
			return invalid("an event filter's actions must hold at least one action")
		}
	}
	filters := normalizeFilters(s.Events)
	if len(filters) < 1 || len(filters) > 32 {
		return invalid("events must hold 1 to 32 filters")
	}
	for _, filter := range filters {
		if !slices.Contains(allowed, filter.Event) {
			return invalid("event %q is not supported for this GitHub subject", filter.Event)
		}
		if filter.Event == EventMergeabilityChanged && filter.Actions != nil {
			return invalid("event %q does not accept actions", filter.Event)
		}
		if filter.Actions == nil {
			continue
		}
		if len(filter.Actions) > 32 {
			return invalid("an event filter holds at most 32 actions")
		}
		if slices.Contains(refEvents, filter.Event) || filter.Event == EventStatus {
			return invalid("event %q has no action selector", filter.Event)
		}
		for _, action := range filter.Actions {
			if len(action) > 64 || !actionPattern.MatchString(action) {
				return invalid("an action is 1 to 64 lowercase letters and underscores")
			}
		}
	}
	return nil
}

// normalizeFilters is the filters as a set in the order they are stored: by event name, then by actions,
// each filter's actions sorted and without repeats.
func normalizeFilters(filters []EventFilter) []EventFilter {
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
		sourcesEqual(s.Source, other.Source)
}

func sourcesEqual(a, b Source) bool {
	switch a := a.(type) {
	case ActionsSource:
		b, ok := b.(ActionsSource)
		return ok && a == b
	case GitHubSource:
		b, ok := b.(GitHubSource)
		if !ok || a.Repository != b.Repository || a.Subject != b.Subject || (a.Events == nil) != (b.Events == nil) {
			return false
		}
		return slices.EqualFunc(normalizeFilters(a.Events), normalizeFilters(b.Events), func(x, y EventFilter) bool {
			return x.Event == y.Event && (x.Actions == nil) == (y.Actions == nil) && slices.Equal(x.Actions, y.Actions)
		})
	default:
		return false
	}
}

// DestinationKey is the identity of an owner's inbox for a Session: the SHA-256 of the JSON that Python's
// `json.dumps([destination_ref, session_id], sort_keys=True)` writes, in lowercase hexadecimal. Existing
// inboxes are found by it, so any other serialization makes each of them look new.
func DestinationKey(ref DestinationRef, sessionID string) string {
	var serialized strings.Builder
	serialized.WriteString(`[{"name": `)
	writePythonString(&serialized, ref.Name)
	serialized.WriteString(`, "namespace": `)
	writePythonString(&serialized, ref.Namespace)
	serialized.WriteString(`, "uid": `)
	writePythonString(&serialized, ref.UID)
	serialized.WriteString(`}, `)
	writePythonString(&serialized, sessionID)
	serialized.WriteString(`]`)
	sum := sha256.Sum256([]byte(serialized.String()))
	return hex.EncodeToString(sum[:])
}

// writePythonString writes s as `json.dumps` does with its default `ensure_ascii`: everything outside space to
// tilde is escaped, in lowercase hexadecimal, with surrogate pairs above the basic plane.
func writePythonString(out *strings.Builder, s string) {
	out.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"':
			out.WriteString(`\"`)
		case r == '\\':
			out.WriteString(`\\`)
		case r == '\n':
			out.WriteString(`\n`)
		case r == '\r':
			out.WriteString(`\r`)
		case r == '\t':
			out.WriteString(`\t`)
		case r == '\b':
			out.WriteString(`\b`)
		case r == '\f':
			out.WriteString(`\f`)
		case r >= ' ' && r <= '~':
			out.WriteRune(r)
		case r > 0xFFFF:
			r -= 0x10000
			fmt.Fprintf(out, `\u%04x\u%04x`, 0xD800+(r>>10), 0xDC00+(r&0x3FF))
		default:
			fmt.Fprintf(out, `\u%04x`, r)
		}
	}
	out.WriteByte('"')
}

// The JSON forms below are those Pydantic writes, which the rows already hold.

type filterJSON struct {
	Event   EventName `json:"event"`
	Actions []string  `json:"actions"`
}

func (s Subscribe) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		DestinationRef DestinationRef `json:"destination_ref"`
		SessionID      string         `json:"session_id"`
		IdempotencyKey string         `json:"idempotency_key"`
		Source         Source         `json:"source"`
		LifetimeDays   int64          `json:"lifetime_days"`
	}{s.DestinationRef, s.SessionID, s.IdempotencyKey, s.Source, s.LifetimeDays})
}

func (s ActionsSource) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Provider      string    `json:"provider"`
		RequestID     uuid.UUID `json:"request_id"`
		AfterSequence int64     `json:"after_sequence"`
	}{"actions", s.RequestID, s.AfterSequence})
}

func (s GitHubSource) MarshalJSON() ([]byte, error) {
	var events []filterJSON
	if s.Events != nil {
		events = []filterJSON{}
		for _, filter := range normalizeFilters(s.Events) {
			events = append(events, filterJSON{filter.Event, filter.Actions})
		}
	}
	return json.Marshal(struct {
		Provider   string        `json:"provider"`
		Repository string        `json:"repository"`
		Subject    GitHubSubject `json:"subject"`
		Events     []filterJSON  `json:"events"`
	}{"github", s.Repository, s.Subject, events})
}

func (s PullRequestSubject) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Kind   string `json:"kind"`
		Number int64  `json:"number"`
	}{"pull_request", s.Number})
}

func (s IssueSubject) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Kind   string `json:"kind"`
		Number int64  `json:"number"`
	}{"issue", s.Number})
}

func (s BranchSubject) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Kind string `json:"kind"`
		Name string `json:"name"`
	}{"branch", s.Name})
}

func (s CommitSubject) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Kind string `json:"kind"`
		SHA  string `json:"sha"`
	}{"commit", s.SHA})
}

func (e ActionsEvent) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Provider  string    `json:"provider"`
		RequestID uuid.UUID `json:"request_id"`
		Sequence  int64     `json:"sequence"`
	}{"actions", e.RequestID, e.Sequence})
}

func (e GitHubEvent) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Provider     string    `json:"provider"`
		AppID        int64     `json:"app_id"`
		DeliveryID   uuid.UUID `json:"delivery_id"`
		RepositoryID int64     `json:"repository_id"`
		Event        EventName `json:"event"`
		Action       *string   `json:"action"`
	}{"github", e.AppID, e.DeliveryID, e.RepositoryID, e.Event, e.Action})
}

func (e GitHubMergeabilityEvent) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Provider          string `json:"provider"`
		AppID             int64  `json:"app_id"`
		RepositoryID      int64  `json:"repository_id"`
		PullRequestNumber int64  `json:"pull_request_number"`
		Sequence          int64  `json:"sequence"`
	}{"github_mergeability", e.AppID, e.RepositoryID, e.PullRequestNumber, e.Sequence})
}

func (e GitHubMergeabilitySnapshotEvent) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Provider          string `json:"provider"`
		AppID             int64  `json:"app_id"`
		RepositoryID      int64  `json:"repository_id"`
		PullRequestNumber int64  `json:"pull_request_number"`
		SnapshotVersion   int64  `json:"snapshot_version"`
	}{"github_mergeability_snapshot", e.AppID, e.RepositoryID, e.PullRequestNumber, e.Version})
}

func (e MessageEvent) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Provider  string         `json:"provider"`
		MessageID uuid.UUID      `json:"message_id"`
		Sender    ServiceAccount `json:"sender"`
	}{"message", e.MessageID, e.Sender})
}

// serviceAccountJSON is the historical Notification event wire shape. Unlike the shared validated identity codec,
// this stored-event reader accepts absent or empty namespace/name fields.
type serviceAccountJSON ServiceAccount

// decode reads one JSON value into v, refusing fields v does not name, as the stored models do.
func decode(data []byte, v any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(v); err != nil {
		return err
	}
	if decoder.More() {
		return errors.New("trailing data after the JSON value")
	}
	return nil
}

// required reports a field that was absent or null.
func required(field string, present bool) error {
	if !present {
		return fmt.Errorf("%s is required", field)
	}
	return nil
}

func (s *Subscribe) UnmarshalJSON(data []byte) error {
	var wire struct {
		DestinationRef *DestinationRef `json:"destination_ref"`
		SessionID      *string         `json:"session_id"`
		IdempotencyKey *string         `json:"idempotency_key"`
		Source         json.RawMessage `json:"source"`
		LifetimeDays   *int64          `json:"lifetime_days"`
	}
	if err := decode(data, &wire); err != nil {
		return err
	}
	if err := errors.Join(
		required("destination_ref", wire.DestinationRef != nil),
		required("session_id", wire.SessionID != nil),
		required("idempotency_key", wire.IdempotencyKey != nil),
		required("source", len(wire.Source) > 0),
	); err != nil {
		return err
	}
	source, err := decodeSource(wire.Source)
	if err != nil {
		return err
	}
	*s = Subscribe{
		DestinationRef: *wire.DestinationRef, SessionID: *wire.SessionID, IdempotencyKey: *wire.IdempotencyKey,
		Source: source, LifetimeDays: 7,
	}
	if wire.LifetimeDays != nil {
		s.LifetimeDays = *wire.LifetimeDays
	}
	return nil
}

func decodeSource(data []byte) (Source, error) {
	var head struct {
		Provider string `json:"provider"`
	}
	if err := json.Unmarshal(data, &head); err != nil {
		return nil, err
	}
	switch head.Provider {
	case "actions":
		var wire struct {
			Provider      string     `json:"provider"`
			RequestID     *uuid.UUID `json:"request_id"`
			AfterSequence *int64     `json:"after_sequence"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := required("request_id", wire.RequestID != nil); err != nil {
			return nil, err
		}
		source := ActionsSource{RequestID: *wire.RequestID}
		if wire.AfterSequence != nil {
			source.AfterSequence = *wire.AfterSequence
		}
		return source, nil
	case "github":
		var wire struct {
			Provider   string          `json:"provider"`
			Repository *string         `json:"repository"`
			Subject    json.RawMessage `json:"subject"`
			Events     []filterJSON    `json:"events"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(required("repository", wire.Repository != nil), required("subject", len(wire.Subject) > 0)); err != nil {
			return nil, err
		}
		subject, err := decodeSubject(wire.Subject)
		if err != nil {
			return nil, err
		}
		source := GitHubSource{Repository: *wire.Repository, Subject: subject}
		if wire.Events != nil {
			source.Events = make([]EventFilter, len(wire.Events))
			for i, filter := range wire.Events {
				source.Events[i] = EventFilter{Event: filter.Event, Actions: filter.Actions}
			}
		}
		return source, nil
	default:
		return nil, fmt.Errorf("unknown source provider %q", head.Provider)
	}
}

func decodeSubject(data []byte) (GitHubSubject, error) {
	var head struct {
		Kind string `json:"kind"`
	}
	if err := json.Unmarshal(data, &head); err != nil {
		return nil, err
	}
	switch head.Kind {
	case "pull_request", "issue":
		var wire struct {
			Kind   string `json:"kind"`
			Number *int64 `json:"number"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := required("number", wire.Number != nil); err != nil {
			return nil, err
		}
		if head.Kind == "issue" {
			return IssueSubject{Number: *wire.Number}, nil
		}
		return PullRequestSubject{Number: *wire.Number}, nil
	case "branch":
		var wire struct {
			Kind string  `json:"kind"`
			Name *string `json:"name"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := required("name", wire.Name != nil); err != nil {
			return nil, err
		}
		return BranchSubject{Name: *wire.Name}, nil
	case "commit":
		var wire struct {
			Kind string  `json:"kind"`
			SHA  *string `json:"sha"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := required("sha", wire.SHA != nil); err != nil {
			return nil, err
		}
		return CommitSubject{SHA: *wire.SHA}, nil
	default:
		return nil, fmt.Errorf("unknown subject kind %q", head.Kind)
	}
}

// DecodeEventIdentity reads the JSON of an entry's `event` column.
func DecodeEventIdentity(data []byte) (EventIdentity, error) {
	var head struct {
		Provider string `json:"provider"`
	}
	if err := json.Unmarshal(data, &head); err != nil {
		return nil, err
	}
	switch head.Provider {
	case "actions":
		var wire struct {
			Provider  string     `json:"provider"`
			RequestID *uuid.UUID `json:"request_id"`
			Sequence  *int64     `json:"sequence"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(required("request_id", wire.RequestID != nil), required("sequence", wire.Sequence != nil)); err != nil {
			return nil, err
		}
		return ActionsEvent{RequestID: *wire.RequestID, Sequence: *wire.Sequence}, nil
	case "github":
		var wire struct {
			Provider     string     `json:"provider"`
			AppID        *int64     `json:"app_id"`
			DeliveryID   *uuid.UUID `json:"delivery_id"`
			RepositoryID *int64     `json:"repository_id"`
			Event        EventName  `json:"event"`
			Action       *string    `json:"action"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(
			required("app_id", wire.AppID != nil),
			required("delivery_id", wire.DeliveryID != nil),
			required("repository_id", wire.RepositoryID != nil),
		); err != nil {
			return nil, err
		}
		if !slices.Contains(knownEvents, wire.Event) {
			return nil, fmt.Errorf("unknown GitHub event %q", wire.Event)
		}
		return GitHubEvent{
			AppID: *wire.AppID, DeliveryID: *wire.DeliveryID, RepositoryID: *wire.RepositoryID,
			Event: wire.Event, Action: wire.Action,
		}, nil
	case "github_mergeability":
		var wire struct {
			Provider          string `json:"provider"`
			AppID             *int64 `json:"app_id"`
			RepositoryID      *int64 `json:"repository_id"`
			PullRequestNumber *int64 `json:"pull_request_number"`
			Sequence          *int64 `json:"sequence"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(
			required("app_id", wire.AppID != nil),
			required("repository_id", wire.RepositoryID != nil),
			required("pull_request_number", wire.PullRequestNumber != nil),
			required("sequence", wire.Sequence != nil),
		); err != nil {
			return nil, err
		}
		return GitHubMergeabilityEvent{
			AppID: *wire.AppID, RepositoryID: *wire.RepositoryID,
			PullRequestNumber: *wire.PullRequestNumber, Sequence: *wire.Sequence,
		}, nil
	case "github_mergeability_snapshot":
		var wire struct {
			Provider          string `json:"provider"`
			AppID             *int64 `json:"app_id"`
			RepositoryID      *int64 `json:"repository_id"`
			PullRequestNumber *int64 `json:"pull_request_number"`
			SnapshotVersion   *int64 `json:"snapshot_version"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(
			required("app_id", wire.AppID != nil),
			required("repository_id", wire.RepositoryID != nil),
			required("pull_request_number", wire.PullRequestNumber != nil),
			required("snapshot_version", wire.SnapshotVersion != nil),
		); err != nil {
			return nil, err
		}
		return GitHubMergeabilitySnapshotEvent{
			AppID: *wire.AppID, RepositoryID: *wire.RepositoryID,
			PullRequestNumber: *wire.PullRequestNumber, Version: *wire.SnapshotVersion,
		}, nil
	case "message":
		var wire struct {
			Provider  string              `json:"provider"`
			MessageID *uuid.UUID          `json:"message_id"`
			Sender    *serviceAccountJSON `json:"sender"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(required("message_id", wire.MessageID != nil), required("sender", wire.Sender != nil)); err != nil {
			return nil, err
		}
		return MessageEvent{MessageID: *wire.MessageID, Sender: ServiceAccount(*wire.Sender)}, nil
	default:
		return nil, fmt.Errorf("unknown event provider %q", head.Provider)
	}
}
