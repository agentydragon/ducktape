package reads

import (
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// sandboxName is a Sandbox name no other test's Sessions have.
func sandboxName(prefix string) string { return prefix + uuid.NewString()[:8] }

func idsOf(sessionIDs ...uuid.UUID) []string {
	ids := make([]string, len(sessionIDs))
	for index, sessionID := range sessionIDs {
		ids[index] = sessionID.String()
	}
	return ids
}

func batch(t *testing.T, f fixture, sessionIDs ...string) map[string]*historypb.SessionSummary {
	t.Helper()
	response, err := f.reader.BatchGetSessionSummaries(t.Context(), &historypb.BatchGetSessionSummariesRequest{SessionIds: sessionIDs})
	if err != nil {
		t.Fatal(err)
	}
	summaries := map[string]*historypb.SessionSummary{}
	for _, summary := range response.Summaries {
		summaries[summary.SessionId] = summary
	}
	return summaries
}

// startedAt is a harness start as the first entry of a Session, `after` the test origin.
func startedAt(after time.Duration) *eventlogpb.EventEntry {
	started := harnessStarted(1)
	started.Event.At = timestamppb.New(testOrigin.Add(after))
	return started
}

func TestASummaryReportsTheFoldBesideCommittedHistory(t *testing.T) {
	f := newFixture(t)
	sandbox := sandboxName("folded-")
	folded := f.session(t, f.history, sandbox, startedAt(20*time.Minute))
	f.fold(t, folded)
	// Committed after the fold ran, so the fold lags it.
	lagging := f.session(t, f.history, sandboxName("lagging-"), startedAt(30*time.Minute))

	summaries := batch(t, f, idsOf(folded, lagging)...)

	got := summaries[folded.String()]
	if got.GetProjectionEpoch() != testEpoch || got.FoldedThrough != 1 || got.CommittedCursor != 1 {
		t.Fatalf("folded summary = %v", got)
	}
	if !got.FirstEventAt.AsTime().Equal(testOrigin.Add(20 * time.Minute)) {
		t.Fatalf("first event at %v", got.FirstEventAt.AsTime())
	}
	if got.Sandbox.GetName() != sandbox || got.Sandbox.GetNamespace() != "test-sandbox-namespace" {
		t.Fatalf("sandbox = %v", got.Sandbox)
	}
	lag := summaries[lagging.String()]
	if lag.ProjectionEpoch != nil || lag.FoldedThrough != 0 || lag.CommittedCursor != 1 || lag.FirstEventAt != nil {
		t.Fatalf("lagging summary = %v", lag)
	}
}

func TestSummaryFieldsComeFromTheirColumns(t *testing.T) {
	f := newFixture(t)
	sessionID := f.session(t, f.history, sandboxName("columns-"))
	moment := func(seconds int) time.Time { return testOrigin.Add(time.Duration(seconds) * time.Second) }
	if _, err := f.db.Exec(`UPDATE thread_history_summary SET first_event_at = $2, last_event_at = $3,
		last_model_activity_at = $4, idle_since = $5, last_turn_status = $6 WHERE thread_id = $1`,
		sessionID, moment(1), moment(2), moment(3), moment(4), int32(eventpb.TurnStatus_TURN_STATUS_COMPLETED)); err != nil {
		t.Fatal(err)
	}

	got := batch(t, f, sessionID.String())[sessionID.String()]

	times := []time.Time{got.FirstEventAt.AsTime(), got.LastEventAt.AsTime(), got.LastModelActivityAt.AsTime(), got.IdleSince.AsTime()}
	if want := []time.Time{moment(1), moment(2), moment(3), moment(4)}; !slices.EqualFunc(times, want, time.Time.Equal) {
		t.Fatalf("first, last, model activity, idle = %v, want %v", times, want)
	}
	if got.GetLastTurnStatus() != eventpb.TurnStatus_TURN_STATUS_COMPLETED {
		t.Fatalf("last turn status = %v", got.LastTurnStatus)
	}
}

func TestTheFeedStatusFollowsHowTheStreamEnded(t *testing.T) {
	f := newFixture(t)
	text := func(document string) *string { return &document }
	attached, err := protojson.Marshal(&runnerpb.Attached{LastCursor: 3})
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name       string
		attached   *string
		end        *string
		wantStatus historypb.FeedStatus
		wantError  string
		wantCursor uint64
	}{
		{name: "no attachment yet", wantStatus: historypb.FeedStatus_FEED_STATUS_UNSPECIFIED},
		{name: "running", attached: text(string(attached)), wantStatus: historypb.FeedStatus_FEED_STATUS_ACTIVE, wantCursor: 3},
		{name: "ended cleanly", attached: text(string(attached)), end: text("{}"), wantStatus: historypb.FeedStatus_FEED_STATUS_ENDED, wantCursor: 3},
		{
			name: "failed", attached: text(string(attached)), end: text(`{"message": "test rejection"}`),
			wantStatus: historypb.FeedStatus_FEED_STATUS_FAILED, wantError: "test rejection", wantCursor: 3,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			sessionID := f.session(t, f.history, sandboxName("feed-"))
			if _, err := f.db.Exec(`UPDATE thread_history_summary SET attached = $2::jsonb, "end" = $3::jsonb WHERE thread_id = $1`,
				sessionID, test.attached, test.end); err != nil {
				t.Fatal(err)
			}

			got, err := f.reader.GetSessionSummary(t.Context(), &historypb.GetSessionSummaryRequest{SessionId: sessionID.String()})
			if err != nil {
				t.Fatal(err)
			}

			if got.FeedStatus != test.wantStatus || got.GetFeedError() != test.wantError || (got.FeedError != nil) != (test.wantError != "") {
				t.Fatalf("feed status %v error %v, want %v %v", got.FeedStatus, got.FeedError, test.wantStatus, test.wantError)
			}
			if (got.Attached != nil) != (test.wantCursor != 0) || got.Attached.GetLastCursor() != test.wantCursor {
				t.Fatalf("attached = %v", got.Attached)
			}
		})
	}
}

func TestABatchReadLeavesOutSessionsWithoutHistory(t *testing.T) {
	f := newFixture(t)
	named := []uuid.UUID{
		f.session(t, f.history, sandboxName("named-"), harnessStarted(1)),
		f.session(t, f.history, sandboxName("named-"), harnessStarted(1)),
	}
	f.session(t, f.history, sandboxName("unnamed-"), harnessStarted(1))

	summaries := batch(t, f, append(idsOf(named...), named[0].String(), uuid.NewString())...)

	if len(summaries) != 2 || summaries[named[0].String()] == nil || summaries[named[1].String()] == nil {
		t.Fatalf("summaries = %v, want the two named Sessions once each", summaries)
	}
}

func TestABatchReadIsBoundedByTheSessionsItNames(t *testing.T) {
	f := newFixture(t)
	distinct := func(count int) []string {
		ids := make([]string, count)
		for index := range ids {
			ids[index] = uuid.NewString()
		}
		return ids
	}
	repeated := slices.Repeat([]string{uuid.NewString()}, 201)

	if _, err := f.reader.BatchGetSessionSummaries(t.Context(), &historypb.BatchGetSessionSummariesRequest{SessionIds: distinct(200)}); err != nil {
		t.Errorf("200 Sessions: %v", err)
	}
	if _, err := f.reader.BatchGetSessionSummaries(t.Context(), &historypb.BatchGetSessionSummariesRequest{SessionIds: repeated}); err != nil {
		t.Errorf("one Session named 201 times: %v", err)
	}
	if _, err := f.reader.BatchGetSessionSummaries(t.Context(), &historypb.BatchGetSessionSummariesRequest{SessionIds: distinct(201)}); !errors.Is(err, storage.ErrInvalidArgument) {
		t.Errorf("201 Sessions: %v, want ErrInvalidArgument", err)
	}
}

func TestASingleSummaryNeedsAnExistingSession(t *testing.T) {
	f := newFixture(t)
	for _, test := range []struct {
		sessionID string
		want      error
	}{{uuid.NewString(), storage.ErrNotFound}, {"not-a-uuid", storage.ErrInvalidArgument}} {
		_, err := f.reader.GetSessionSummary(t.Context(), &historypb.GetSessionSummaryRequest{SessionId: test.sessionID})
		if !errors.Is(err, test.want) {
			t.Errorf("summary of %q: %v, want %v", test.sessionID, err, test.want)
		}
	}
}

func findIDs(t *testing.T, f fixture, request *historypb.FindSessionsRequest) []string {
	t.Helper()
	response, err := f.reader.FindSessions(t.Context(), request)
	if err != nil {
		t.Fatal(err)
	}
	var ids []string
	for _, found := range response.Sessions {
		if found.Sandbox.Name == "" || found.Sandbox.Namespace != "test-sandbox-namespace" {
			t.Fatalf("session %s has sandbox %v", found.SessionId, found.Sandbox)
		}
		ids = append(ids, found.SessionId)
	}
	slices.Sort(ids)
	return ids
}

func TestSessionsAreFoundByTheNameOfTheSandboxThatWroteThem(t *testing.T) {
	f := newFixture(t)
	unique := uuid.NewString()[:8]
	// Text with LIKE wildcards must match only itself.
	wildcards := f.session(t, f.history, "wild_%-"+unique)
	f.session(t, f.history, "wildXY-"+unique)
	plain := f.session(t, f.history, "plain-"+unique)

	exact := findIDs(t, f, &historypb.FindSessionsRequest{Sandbox: &historypb.FindSessionsRequest_SandboxName{SandboxName: "plain-" + unique}})
	wrongCase := findIDs(t, f, &historypb.FindSessionsRequest{Sandbox: &historypb.FindSessionsRequest_SandboxName{SandboxName: "PLAIN-" + unique}})
	partial := findIDs(t, f, &historypb.FindSessionsRequest{Sandbox: &historypb.FindSessionsRequest_SandboxNameContains{SandboxNameContains: "LAIN-" + unique}})
	literal := findIDs(t, f, &historypb.FindSessionsRequest{Sandbox: &historypb.FindSessionsRequest_SandboxNameContains{SandboxNameContains: strings.ToUpper("ild_%-" + unique)}})

	if !slices.Equal(exact, []string{plain.String()}) || len(wrongCase) != 0 || !slices.Equal(partial, exact) {
		t.Fatalf("exact %v, exact in another case %v, partial %v; want only %s", exact, wrongCase, partial, plain)
	}
	if !slices.Equal(literal, []string{wildcards.String()}) {
		t.Fatalf("a name containing wildcards found %v, want only %s", literal, wildcards)
	}
}

func TestSessionsExposeAndFilterBySandboxUID(t *testing.T) {
	f := newFixture(t)
	first, second := uuid.New(), uuid.New()
	for index, sandboxUID := range []string{"sandbox-uid-first", "sandbox-uid-replacement"} {
		sessionID := []uuid.UUID{first, second}[index]
		writer := storage.Writer{
			Namespace: "test-sandbox-namespace", Name: "same-name", UID: "writer-account-" + sandboxUID,
		}
		if _, err := f.history.BindWriter(t.Context(), sessionID, writer, sandboxUID, nil); err != nil {
			t.Fatal(err)
		}
		summary, err := f.reader.GetSessionSummary(t.Context(), &historypb.GetSessionSummaryRequest{SessionId: sessionID.String()})
		if err != nil {
			t.Fatal(err)
		}
		if got := summary.GetSandbox().GetUid(); got != sandboxUID {
			t.Errorf("summary UID = %q, want %q", got, sandboxUID)
		}
		found := findIDs(t, f, &historypb.FindSessionsRequest{
			Sandbox: &historypb.FindSessionsRequest_SandboxUid{SandboxUid: sandboxUID},
		})
		if !slices.Equal(found, []string{sessionID.String()}) {
			t.Errorf("FindSessions(%q) = %v, want only %s", sandboxUID, found, sessionID)
		}
	}
}

func TestFindingSessionsNeedsANonEmptyName(t *testing.T) {
	f := newFixture(t)
	for name, request := range map[string]*historypb.FindSessionsRequest{
		"no match":       {},
		"empty exact":    {Sandbox: &historypb.FindSessionsRequest_SandboxName{}},
		"empty contains": {Sandbox: &historypb.FindSessionsRequest_SandboxNameContains{}},
		"empty UID":      {Sandbox: &historypb.FindSessionsRequest_SandboxUid{}},
	} {
		if _, err := f.reader.FindSessions(t.Context(), request); !errors.Is(err, storage.ErrInvalidArgument) {
			t.Errorf("%s: %v, want ErrInvalidArgument", name, err)
		}
	}
}
