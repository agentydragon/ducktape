package store

import (
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
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

func TestSummariesExposeOnlyStructuredLifecycleInformation(t *testing.T) {
	request := uuid.New()
	event := model.ActionsEvent{RequestID: request, Sequence: 3}
	summary, err := summarize(1, event, []byte(`{"state":"decision_pending","actor":"private"}`), time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	requireEqual(t, summary.Event, event)
	requireEqual(t, summary.ActionState, ptr("decision_pending"))
	github := model.GitHubEvent{Event: model.EventCheckRun}
	summary, err = summarize(2, github, []byte(`{"check_run":{"conclusion":"success","output":"private"}}`), time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	requireEqual(t, summary.Event, github)
	requireEqual(t, summary.GitHubConclusion, ptr("success"))
	summary, err = summarize(2, github, []byte(`{"check_run":{"conclusion":"private arbitrary text"}}`), time.Time{})
	if err != nil || summary.GitHubConclusion != nil {
		t.Fatalf("unsafe conclusion: %v, %v", summary, err)
	}
	message := model.MessageEvent{MessageID: request, Sender: model.ServiceAccount{Namespace: "test-namespace", Name: "test-sender"}}
	summary, err = summarize(3, message, []byte(`{"body":"private"}`), time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	requireEqual(t, summary.Event, message)
}

func TestAPayloadThatIsNotJSONFailsTheSummary(t *testing.T) {
	if _, err := summarize(4, model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}, []byte(`not json`), time.Time{}); err == nil {
		t.Fatal("summarize accepted a payload that is not JSON")
	}
}
