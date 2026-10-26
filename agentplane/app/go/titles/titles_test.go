package titles

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/historywatch"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

type fakeWatchScript struct {
	watchErr  error
	responses []*historypb.WatchSessionSummariesResponse
	recvErr   error
}

type fakeHistory struct {
	mu                sync.Mutex
	watchScripts      []fakeWatchScript
	watchRequests     []*historypb.WatchSessionSummariesRequest
	redials           int
	observations      *historypb.ReadSessionObservationsResponse
	observationsErr   error
	observationsInput []*historypb.ReadSessionObservationsRequest
	readEvents        func(*historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error)
	eventRequests     []*historypb.ReadSessionEventsRequest
}

func (h *fakeHistory) WatchSessionSummaries(ctx context.Context, request *historypb.WatchSessionSummariesRequest) (historywatch.Stream, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	requestCopy := *request
	if request.OpenedAfter != nil {
		value := *request.OpenedAfter
		requestCopy.OpenedAfter = &value
	}
	h.watchRequests = append(h.watchRequests, &requestCopy)
	if len(h.watchScripts) == 0 {
		return &fakeWatchStream{ctx: ctx}, nil
	}
	script := h.watchScripts[0]
	h.watchScripts = h.watchScripts[1:]
	if script.watchErr != nil {
		return nil, script.watchErr
	}
	return &fakeWatchStream{ctx: ctx, responses: script.responses, err: script.recvErr}, nil
}

func (h *fakeHistory) Redial() {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.redials++
}

func (h *fakeHistory) ReadSessionObservations(_ context.Context, request *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.observationsInput = append(h.observationsInput, request)
	if h.observationsErr != nil {
		return nil, h.observationsErr
	}
	return h.observations, nil
}

func (h *fakeHistory) ReadSessionEvents(_ context.Context, request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
	h.mu.Lock()
	copy := *request
	h.eventRequests = append(h.eventRequests, &copy)
	read := h.readEvents
	h.mu.Unlock()
	if read == nil {
		return nil, errors.New("unexpected History event read")
	}
	return read(request)
}

func (h *fakeHistory) watchSnapshot() (int, int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.watchRequests), h.redials
}

func (h *fakeHistory) watchRequestSnapshot() []*historypb.WatchSessionSummariesRequest {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]*historypb.WatchSessionSummariesRequest(nil), h.watchRequests...)
}

func (h *fakeHistory) eventRequestSnapshot() []*historypb.ReadSessionEventsRequest {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]*historypb.ReadSessionEventsRequest(nil), h.eventRequests...)
}

type fakeWatchStream struct {
	ctx       context.Context
	responses []*historypb.WatchSessionSummariesResponse
	err       error
}

func (s *fakeWatchStream) Recv() (*historypb.WatchSessionSummariesResponse, error) {
	if len(s.responses) > 0 {
		response := s.responses[0]
		s.responses = s.responses[1:]
		return response, nil
	}
	if s.err != nil {
		return nil, s.err
	}
	<-s.ctx.Done()
	return nil, s.ctx.Err()
}

type fakeStore struct {
	thread            *threads.Thread
	getErr            error
	revision          int
	titleContextErr   error
	pending           []threads.PendingTitle
	pendingErr        error
	advances          []int64
	advanceErr        error
	claimedRevision   *int
	claimCursor       int64
	claimErr          error
	generatedTitle    string
	generatedRevision int
	generatedErr      error
	applyResult       bool
	applyRevision     int
	applyTitle        string
	applyErr          error
	failRevision      *int
	failErr           error
}

func (s *fakeStore) GetThread(context.Context, uuid.UUID) (*threads.Thread, error) {
	return s.thread, s.getErr
}

func (s *fakeStore) TitleContext(context.Context, uuid.UUID) (int, *string, error) {
	return s.revision, nil, s.titleContextErr
}

func (s *fakeStore) PendingAutoTitles(context.Context) ([]threads.PendingTitle, error) {
	return s.pending, s.pendingErr
}

func (s *fakeStore) AdvanceTitleScanCursor(_ context.Context, _ uuid.UUID, cursor int64) error {
	s.advances = append(s.advances, cursor)
	return s.advanceErr
}

func (s *fakeStore) ClaimAutoTitle(_ context.Context, _ uuid.UUID, cursor int64) (*int, error) {
	s.claimCursor = cursor
	return s.claimedRevision, s.claimErr
}

func (s *fakeStore) GeneratedTitle(_ context.Context, _ uuid.UUID, title string, revision int) (threads.Thread, error) {
	s.generatedTitle, s.generatedRevision = title, revision
	return threads.Thread{Name: &title, TitleRevision: revision + 1}, s.generatedErr
}

func (s *fakeStore) ApplyFirstMessageTitle(_ context.Context, _ uuid.UUID, title string, revision int) (bool, error) {
	s.applyTitle, s.applyRevision = title, revision
	return s.applyResult, s.applyErr
}

func (s *fakeStore) FailAutoTitle(_ context.Context, _ uuid.UUID, revision int) error {
	s.failRevision = &revision
	return s.failErr
}

type fakeBackend struct {
	enabled bool
	title   string
	err     error
	calls   [][]string
}

func (g *fakeBackend) Enabled() bool { return g.enabled }
func (g *fakeBackend) Generate(_ context.Context, messages []string) (string, error) {
	g.calls = append(g.calls, append([]string(nil), messages...))
	return g.title, g.err
}

func quietLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func changedWatchResponse() *historypb.WatchSessionSummariesResponse {
	return &historypb.WatchSessionSummariesResponse{
		Observation: &historypb.WatchSessionSummariesResponse_Changed{Changed: &emptypb.Empty{}},
	}
}

func reconnectWatchResponse() *historypb.WatchSessionSummariesResponse {
	return &historypb.WatchSessionSummariesResponse{
		Observation: &historypb.WatchSessionSummariesResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}},
	}
}

func waitFor(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("condition was not met before timeout")
}

func TestGeneratorPostsRequestAndCleansOutputText(t *testing.T) {
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("secret-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	var requestBody map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method = %s, want POST", r.Method)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer secret-token" {
			t.Errorf("Authorization = %q", got)
		}
		if got := r.Header.Get("Content-Type"); got != "application/json" {
			t.Errorf("Content-Type = %q", got)
		}
		if err := json.NewDecoder(r.Body).Decode(&requestBody); err != nil {
			t.Errorf("decode request: %v", err)
		}
		_, _ = io.WriteString(w, `{"output_text":"  \"Title:  Weekly   status: update.\"  "}`)
	}))
	defer server.Close()
	secretName := "title-token"
	generator := NewGenerator(&GeneratorConfig{Endpoint: server.URL, Model: "title-model", TokenSecretName: &secretName}, server.Client())
	generator.tokenFile = tokenFile
	title, err := generator.Generate(context.Background(), []string{"  first message ", "", "second message"})
	if err != nil {
		t.Fatal(err)
	}
	if title != "Weekly status: update" {
		t.Fatalf("title = %q", title)
	}
	if requestBody["model"] != "title-model" || requestBody["instructions"] != titleInstructions ||
		requestBody["input"] != "Confirmed user messages:\n\nfirst message\n\nsecond message" ||
		requestBody["max_output_tokens"] != float64(48) || requestBody["store"] != false {
		t.Fatalf("request body = %#v", requestBody)
	}
}

func TestGeneratorParsesContentFallbackAndEnforcesCharacterLimits(t *testing.T) {
	var gotInput string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Input string `json:"input"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode request: %v", err)
		}
		gotInput = body.Input
		_, _ = io.WriteString(w, `{"output":[{"type":"reasoning","content":[{"type":"text","text":"ignored"}]},{"type":"message","content":[{"type":"refusal","text":"ignored"},{"type":"output_text","text":"`+strings.Repeat("é", 205)+`"}]}]}`)
	}))
	defer server.Close()
	generator := NewGenerator(&GeneratorConfig{Endpoint: server.URL, Model: "title-model"}, server.Client())
	messageA, messageB := strings.Repeat("a", maxMessageCharacters+1), strings.Repeat("b", maxMessageCharacters+1)
	title, err := generator.Generate(context.Background(), []string{messageA, messageB})
	if err != nil {
		t.Fatal(err)
	}
	if len([]rune(title)) != maxTitleCharacters {
		t.Fatalf("title has %d runes, want %d", len([]rune(title)), maxTitleCharacters)
	}
	contextText := strings.TrimPrefix(gotInput, "Confirmed user messages:\n\n")
	if len([]rune(contextText)) != maxContextCharacters {
		t.Fatalf("context has %d runes, want %d", len([]rune(contextText)), maxContextCharacters)
	}
	if strings.Contains(contextText, strings.Repeat("a", maxMessageCharacters+1)) || !strings.HasSuffix(contextText, strings.Repeat("b", maxMessageCharacters)) {
		t.Fatal("per-message or total context truncation did not match configured limits")
	}
}

func TestGeneratorRequiresNonemptyInputAndToken(t *testing.T) {
	generator := NewGenerator(&GeneratorConfig{Endpoint: "http://example.test", Model: "m"}, nil)
	if _, err := generator.Generate(context.Background(), []string{" \n "}); err == nil {
		t.Fatal("Generate accepted no confirmed messages")
	}
	secret := "secret"
	generator.config.TokenSecretName = &secret
	generator.tokenFile = filepath.Join(t.TempDir(), "missing")
	if _, err := generator.Generate(context.Background(), []string{"hello"}); err == nil {
		t.Fatal("Generate accepted a missing configured token file")
	}
}

func emptyEventEntry(cursor uint64) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Event: &eventpb.Event{}}
}

func confirmedEntry(cursor uint64, text string) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{
		Cursor: cursor,
		Event: &eventpb.Event{
			Observation: &eventpb.Event_HarnessUserMessageConfirmed{
				HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{Text: text},
			},
		},
	}
}

func TestAutoTitleUsesBoundedHistoryWindowAndLastTwelveConfirmedMessages(t *testing.T) {
	id := uuid.New()
	store := &fakeStore{thread: &threads.Thread{ID: id}, revision: 8}
	backend := &fakeBackend{enabled: true, title: "Generated title"}
	var eventsRequest *historypb.ReadSessionEventsRequest
	history := &fakeHistory{
		observations: &historypb.ReadSessionObservationsResponse{LastCursor: 1500},
		readEvents: func(request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
			eventsRequest = request
			entries := make([]*eventlogpb.EventEntry, 0, historyPageSize)
			for cursor := uint64(501); cursor <= 1500; cursor++ {
				entry := emptyEventEntry(cursor)
				if cursor >= 1488 {
					entry = confirmedEntry(cursor, fmt.Sprintf("message-%d", cursor-1487))
				}
				entries = append(entries, entry)
			}
			return &historypb.ReadSessionEventsResponse{LastCursor: 1500, Entries: entries}, nil
		},
	}
	titles, err := NewThreadTitles(backend, store, history, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	thread, err := titles.AutoTitle(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	if thread.Name == nil || *thread.Name != "Generated title" || store.generatedRevision != 8 {
		t.Fatalf("generated store update = %+v, revision %d", thread, store.generatedRevision)
	}
	if eventsRequest == nil || eventsRequest.GetAfterCursor() != 500 || eventsRequest.GetLimit() != historyPageSize {
		t.Fatalf("History event request = %+v, want bounded last-1000 window", eventsRequest)
	}
	if len(backend.calls) != 1 || len(backend.calls[0]) != maxTitleMessages {
		t.Fatalf("generator messages = %+v", backend.calls)
	}
	if backend.calls[0][0] != "message-2" || backend.calls[0][11] != "message-13" {
		t.Fatalf("last twelve messages = %v", backend.calls[0])
	}
}

func TestProcessPendingPagesCursorAndClaimsFirstConfirmedMessage(t *testing.T) {
	id := uuid.New()
	revision := 4
	store := &fakeStore{pending: []threads.PendingTitle{{ID: id, Cursor: 0}}, claimedRevision: &revision, applyResult: true}
	var pages int
	history := &fakeHistory{readEvents: func(request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
		pages++
		if pages == 1 {
			if request.GetAfterCursor() != 0 || request.GetLimit() != historyPageSize {
				return nil, fmt.Errorf("first page request = %+v", request)
			}
			entries := make([]*eventlogpb.EventEntry, historyPageSize)
			for i := range entries {
				entries[i] = emptyEventEntry(uint64(i + 1))
			}
			return &historypb.ReadSessionEventsResponse{LastCursor: 1000, Entries: entries}, nil
		}
		if request.GetAfterCursor() != 1000 {
			return nil, fmt.Errorf("second page cursor = %d, want 1000", request.GetAfterCursor())
		}
		return &historypb.ReadSessionEventsResponse{LastCursor: 1001, Entries: []*eventlogpb.EventEntry{
			confirmedEntry(1001, "  first confirmed message  "),
		}}, nil
	}}
	backend := &fakeBackend{enabled: true, title: "First message title"}
	titles, err := NewThreadTitles(backend, store, history, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	if err := titles.ProcessPending(context.Background()); err != nil {
		t.Fatal(err)
	}
	if pages != 2 || len(store.advances) != 1 || store.advances[0] != 1000 {
		t.Fatalf("pages=%d cursor advances=%v", pages, store.advances)
	}
	if store.claimCursor != 1001 || store.applyRevision != revision || store.applyTitle != backend.title {
		t.Fatalf("claim cursor=%d apply=(%q, revision %d)", store.claimCursor, store.applyTitle, store.applyRevision)
	}
	if len(backend.calls) != 1 || len(backend.calls[0]) != 1 || backend.calls[0][0] != "  first confirmed message  " {
		t.Fatalf("generator calls = %+v", backend.calls)
	}
	if store.failRevision != nil {
		t.Fatalf("successful title generation reset claim at revision %d", *store.failRevision)
	}
}

func TestProcessPendingResetsClaimWhenGenerationFails(t *testing.T) {
	id := uuid.New()
	revision := 9
	store := &fakeStore{pending: []threads.PendingTitle{{ID: id}}, claimedRevision: &revision}
	history := &fakeHistory{readEvents: func(*historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
		return &historypb.ReadSessionEventsResponse{LastCursor: 1, Entries: []*eventlogpb.EventEntry{
			confirmedEntry(1, "first message"),
		}}, nil
	}}
	backend := &fakeBackend{enabled: true, err: errors.New("backend unavailable")}
	titles, err := NewThreadTitles(backend, store, history, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	if err := titles.ProcessPending(context.Background()); err != nil {
		t.Fatal(err)
	}
	if store.failRevision == nil || *store.failRevision != revision {
		t.Fatalf("failure reset revision = %v, want %d", store.failRevision, revision)
	}
	if store.applyRevision != 0 {
		t.Fatal("ApplyFirstMessageTitle ran after generation failed")
	}
}

func TestProcessPendingDoesNothingWhenGenerationIsDisabled(t *testing.T) {
	titles, err := NewThreadTitles(&fakeBackend{}, &fakeStore{}, &fakeHistory{}, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	if err := titles.ProcessPending(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestNilThreadTitlesAreDisabled(t *testing.T) {
	var service *ThreadTitles
	if service.Enabled() {
		t.Fatal("nil title service reported enabled")
	}
}

func TestHistoryNotFoundProducesNoOnDemandMessages(t *testing.T) {
	id := uuid.New()
	backend := &fakeBackend{enabled: true, err: errors.New("a Thread needs a confirmed user message before it can be titled")}
	store := &fakeStore{thread: &threads.Thread{ID: id}}
	history := &fakeHistory{observationsErr: status.Error(codes.NotFound, "not yet pushed")}
	titles, err := NewThreadTitles(backend, store, history, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	_, err = titles.AutoTitle(context.Background(), id)
	if err == nil || !strings.Contains(err.Error(), "confirmed user message") {
		t.Fatalf("AutoTitle error = %v, want empty-confirmed-message error", err)
	}
	if len(backend.calls) != 1 || len(backend.calls[0]) != 0 {
		t.Fatalf("generator calls = %+v, want one empty message list", backend.calls)
	}
}

func TestMissingThreadUsesStoreNotFound(t *testing.T) {
	id := uuid.New()
	titles, err := NewThreadTitles(&fakeBackend{enabled: true}, &fakeStore{}, &fakeHistory{}, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	_, err = titles.AutoTitle(context.Background(), id)
	if !errors.Is(err, threads.ErrNotFound) {
		t.Fatalf("AutoTitle error = %v, want threads.ErrNotFound", err)
	}
}
