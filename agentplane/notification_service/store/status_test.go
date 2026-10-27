package store

import (
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestNoticeWaitFollowsWhatHoldsTheInboxInPriority(t *testing.T) {
	observed := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	debounce := Debounce{Quiet: 2 * time.Second, MaxWait: 10 * time.Second}
	ago := func(d time.Duration) time.Time { return observed.Add(-d) }
	failure := "failed"
	for _, c := range []struct {
		name    string
		inbox   Inbox
		notice  *Notice
		pending *EntryTimes
		want    *NoticeWaitReason
		// window is the deadlines reported while debouncing.
		window *Window
	}{
		{name: "nothing awaits a notice"},
		{name: "a confirmed notice and nothing new", notice: &Notice{Confirmed: true}},
		{
			name:    "a retired inbox beats every other reason",
			inbox:   Inbox{Retired: true, DeliveryError: &failure},
			notice:  &Notice{Error: &failure},
			pending: &EntryTimes{First: ago(time.Second), Last: ago(time.Second)},
			want:    ptr(WaitRetired),
		},
		{
			name:   "a notice error beats a delivery error",
			inbox:  Inbox{DeliveryError: &failure},
			notice: &Notice{Error: &failure},
			want:   ptr(WaitNoticeError),
		},
		{
			name:   "a delivery error beats an unconfirmed notice",
			inbox:  Inbox{DeliveryError: &failure},
			notice: &Notice{Attempted: true},
			want:   ptr(WaitDeliveryRetry),
		},
		{
			name:    "an unconfirmed notice beats the debounce",
			notice:  &Notice{Admitted: true},
			pending: &EntryTimes{First: ago(time.Second), Last: ago(time.Second)},
			want:    ptr(WaitAwaitingConfirmation),
		},
		{
			name:    "entries inside the quiet window",
			notice:  &Notice{Confirmed: true},
			pending: &EntryTimes{First: ago(time.Second), Last: ago(time.Second)},
			want:    ptr(WaitDebouncing),
			window: &Window{
				QuietUntil: observed.Add(time.Second), MaxWaitAt: observed.Add(9 * time.Second), Due: observed.Add(time.Second),
			},
		},
		{
			name:    "the maximum wait caps a busy quiet window",
			pending: &EntryTimes{First: ago(9 * time.Second), Last: ago(0)},
			want:    ptr(WaitDebouncing),
			window: &Window{
				QuietUntil: observed.Add(2 * time.Second), MaxWaitAt: observed.Add(time.Second), Due: observed.Add(time.Second),
			},
		},
		{name: "entries past their deadline", pending: &EntryTimes{First: ago(time.Minute), Last: ago(time.Minute)}, want: ptr(WaitReady)},
		{name: "entries due at this very moment", pending: &EntryTimes{First: ago(2 * time.Second), Last: ago(2 * time.Second)}, want: ptr(WaitReady)},
	} {
		t.Run(c.name, func(t *testing.T) {
			got := noticeWait(c.inbox, c.notice, c.pending, observed, debounce)

			requireEqual(t, got, NoticeWait{Reason: c.want, Window: c.window})
		})
	}
}

func TestEntriesAreSummarizedByTheirLifecycleFieldsOnly(t *testing.T) {
	request := uuid.MustParse("5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60")
	action := func(s string) *string { return &s }
	longer := strings.Repeat("é", maxSummaryField+1)
	for _, c := range []struct {
		name    string
		event   EventIdentity
		payload string
		// want is exactly what an operator is shown, which the CLI prints as it is.
		want string
	}{
		{
			"an Action with its state",
			ActionsEvent{RequestID: request, Sequence: 3},
			`{"state": "decision_pending"}`,
			"Action 5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60 · event 3 · decision_pending",
		},
		{
			"an Action without a state",
			ActionsEvent{RequestID: request, Sequence: 3},
			`{}`,
			"Action 5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60 · event 3",
		},
		{
			"an Action whose state is not text",
			ActionsEvent{RequestID: request, Sequence: 3},
			`{"state": 7}`,
			"Action 5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60 · event 3",
		},
		{
			"an Action state is cut by characters",
			ActionsEvent{RequestID: request, Sequence: 3},
			`{"state": "` + longer + `"}`,
			"Action 5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60 · event 3 · " + strings.Repeat("é", maxSummaryField),
		},
		{"a GitHub event without an action", GitHubEvent{Event: EventPush}, `{"ref": "refs/heads/secret"}`, "GitHub push"},
		{"a GitHub event with an action", GitHubEvent{Event: EventPullRequest, Action: action("opened")}, `{}`, "GitHub pull_request · opened"},
		{"a GitHub event with an empty action", GitHubEvent{Event: EventPullRequest, Action: action("")}, `{}`, "GitHub pull_request"},
		{
			"a GitHub action is cut by characters",
			GitHubEvent{Event: EventIssues, Action: action(longer)},
			`{}`,
			"GitHub issues · " + strings.Repeat("é", maxSummaryField),
		},
		{
			"an Agentplane mergeability observation shows its pull request and sequence",
			GitHubMergeabilityEvent{AppID: 11, RepositoryID: 22, PullRequestNumber: 7, Sequence: 3},
			`{"mergeable": false, "headSha": "private"}`,
			"GitHub PR #7 mergeability · sequence 3",
		},
		{
			"a check run's conclusion",
			GitHubEvent{Event: EventCheckRun, Action: action("completed")},
			`{"check_run": {"conclusion": "failure", "output": "private"}}`,
			"GitHub check_run · completed · failure",
		},
		{
			"a workflow run's conclusion",
			GitHubEvent{Event: EventWorkflowRun, Action: action("completed")},
			`{"workflow_run": {"conclusion": "success"}}`,
			"GitHub workflow_run · completed · success",
		},
		{"a conclusion that is not text", GitHubEvent{Event: EventCheckRun}, `{"check_run": {"conclusion": null}}`, "GitHub check_run"},
		{
			"a conclusion of an event that has none shown",
			GitHubEvent{Event: EventCheckSuite},
			`{"check_suite": {"conclusion": "success"}}`,
			"GitHub check_suite",
		},
		{
			"a message by its sender, never its body",
			MessageEvent{MessageID: request, Sender: ServiceAccount{Namespace: "test-namespace", Name: "test-sender"}},
			`{"body": "private", "from_session": "test-session"}`,
			"Message from test-namespace/test-sender",
		},
	} {
		t.Run(c.name, func(t *testing.T) {
			got, err := summarize(1, c.event, []byte(c.payload), time.Time{})
			if err != nil {
				t.Fatal(err)
			}

			requireEqual(t, got.Summary, c.want)
		})
	}
}

func TestAnEntrySaysWhichKindOfSourceItCameFrom(t *testing.T) {
	for event, want := range map[EventIdentity]EntryProvider{
		ActionsEvent{RequestID: uuid.New(), Sequence: 1}:           EntryFromActions,
		GitHubEvent{Event: EventPush}:                              EntryFromGitHub,
		GitHubMergeabilityEvent{PullRequestNumber: 7, Sequence: 1}: EntryFromGitHub,
		MessageEvent{MessageID: uuid.New()}:                        EntryFromMessage,
	} {
		got, err := summarize(1, event, []byte(`{}`), time.Time{})
		if err != nil {
			t.Fatal(err)
		}
		requireEqual(t, got.Provider, want)
	}
}

func TestAPayloadThatIsNotJSONFailsTheSummary(t *testing.T) {
	if _, err := summarize(4, ActionsEvent{RequestID: uuid.New(), Sequence: 1}, []byte(`not json`), time.Time{}); err == nil {
		t.Fatal("summarize accepted a payload that is not JSON")
	}
}
