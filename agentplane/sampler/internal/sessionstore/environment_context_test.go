package sessionstore

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
)

func messageText(t *testing.T, item responses.Item) string {
	t.Helper()
	var text string
	if err := json.Unmarshal(item.Content, &text); err == nil {
		return text
	}
	parts, err := item.TextParts()
	if err != nil {
		t.Fatalf("message content %s: %v", item.Content, err)
	}
	texts := make([]string, len(parts))
	for index, part := range parts {
		texts[index] = part.Text
	}
	return strings.Join(texts, "")
}

func TestEnvironmentContextPrecedesTurnInputAndFollowsChanges(t *testing.T) {
	store := newTestStore(t)
	today := time.Date(2026, 1, 2, 12, 0, 0, 0, time.Local)
	store.now = func() time.Time { return today }
	session := openSessionForStore(t, store, "")
	owner := claim(t, store, session)
	submit := func(id, text string) []responses.Item {
		t.Helper()
		admitInput(t, store, session.ID, id, text)
		if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
			t.Fatal(err)
		}
		conversation, err := store.Read(t.Context(), owner)
		if err != nil {
			t.Fatal(err)
		}
		return testsupport.ResponsesItems(t, conversation.Context())
	}

	items := submit("first", "first input")
	if len(items) != 2 || messageText(t, items[1]) != "first input" {
		t.Fatalf("first turn items=%+v", items)
	}
	if got := messageText(t, items[0]); items[0].Role != "user" || !strings.Contains(got, "<cwd>/fixture</cwd>") ||
		!strings.Contains(got, "<current_date>2026-01-02</current_date>") || strings.Contains(got, "<shell>") {
		t.Fatalf("initial environment context=%q role=%q", got, items[0].Role)
	}
	if err := store.FinishCommand(t.Context(), owner, session.ID, "first", "done"); err != nil {
		t.Fatal(err)
	}

	bash := "bash"
	if err := store.SetExecutorSession(t.Context(), owner, "fixture-executor-session", &bash); err != nil {
		t.Fatal(err)
	}
	conversation, err := store.PrepareSamplingContext(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	items = testsupport.ResponsesItems(t, conversation.Context())
	var latestEnvironment string
	for _, item := range items {
		text := messageText(t, item)
		if strings.Contains(text, "<environment_context>") {
			latestEnvironment = text
		}
	}
	if !strings.Contains(latestEnvironment, "<shell>bash</shell>") {
		t.Fatalf("learned shell was not rendered before sampling: %q", latestEnvironment)
	}
	unchanged, err := store.PrepareSamplingContext(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if got := len(testsupport.ResponsesItems(t, unchanged.Context())); got != len(items) {
		t.Fatalf("unchanged environment appended %d items", got-len(items))
	}

	today = today.AddDate(0, 0, 1)
	items = submit("second", "second input")
	update := messageText(t, items[len(items)-2])
	if update != "<environment_context>\n  <current_date>2026-01-03</current_date>\n</environment_context>" ||
		messageText(t, items[len(items)-1]) != "second input" {
		t.Fatalf("date rollover update=%q before %+v", update, items[len(items)-1])
	}
}

func TestCompactionRefreshesEnvironmentInNativeOrder(t *testing.T) {
	for _, tc := range []struct {
		api      protocol.APIStyle
		records  []json.RawMessage
		envIndex int
		envRole  string
	}{
		{
			api: protocol.APIStyleResponses, envIndex: 0, envRole: "user",
			records: []json.RawMessage{
				json.RawMessage(`{"type":"compaction","encrypted_content":"opaque-summary"}`),
			},
		},
		{
			api: protocol.APIStyleChatCompletions, envIndex: 3, envRole: "system",
			records: []json.RawMessage{
				json.RawMessage(`{"role":"assistant","content":"compaction summary"}`),
				json.RawMessage(`{"role":"user","content":"kept input"}`),
				json.RawMessage(`{"role":"assistant","content":"kept answer"}`),
			},
		},
		{
			api: protocol.APIStyleAnthropicMessages, envIndex: 3, envRole: "user",
			records: []json.RawMessage{
				json.RawMessage(`{"id":"msg_summary","role":"assistant","content":[{"type":"compaction","content":"compaction summary","signature":"summary-signature"}],"stop_reason":"compaction"}`),
				json.RawMessage(`{"role":"user","content":[{"type":"text","text":"kept input"}]}`),
				json.RawMessage(`{"id":"msg_retain","role":"assistant","content":[{"type":"thinking","thinking":"kept thought","signature":"thinking-signature"},{"type":"text","text":"kept answer"}],"stop_reason":"end_turn"}`),
			},
		},
	} {
		t.Run(string(tc.api), func(t *testing.T) {
			ctx := t.Context()
			checkEnvironment := func(raw json.RawMessage, required ...string) string {
				t.Helper()
				var environment struct {
					Role    string
					Content json.RawMessage
				}
				if err := json.Unmarshal(raw, &environment); err != nil {
					t.Fatal(err)
				}
				content := string(environment.Content)
				if environment.Role != tc.envRole || !strings.Contains(content, "environment_context") {
					t.Fatalf("unexpected environment role or content: %s", raw)
				}
				for _, field := range required {
					if !strings.Contains(content, field) {
						t.Fatalf("environment missing %q: %s", field, raw)
					}
				}
				return content
			}
			store, dsn := newTestStoreWithDSN(t)
			today := time.Date(2026, 1, 2, 12, 0, 0, 0, time.Local)
			store.now = func() time.Time { return today }
			session, err := openStoreSession(t, store, &sessionconfig.Configuration{
				SessionId: uuid.NewString(), Spec: &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"},
			}, "fixture-account", tc.api)
			if err != nil {
				t.Fatal(err)
			}
			owner := claim(t, store, session)
			if err := store.SetExecutorSession(ctx, owner, "original-executor", new("bash")); err != nil {
				t.Fatal(err)
			}
			original, err := store.PrepareSamplingContext(ctx, owner)
			if err != nil {
				t.Fatal(err)
			}
			initial, err := original.Context().Records()
			if err != nil || len(initial) != 1 {
				t.Fatalf("initial environment records: %s err=%v", initial, err)
			}
			checkEnvironment(initial[0], "/fixture", "2026-01-02", "bash")
			initialOrigin := original.Context().OriginAt(0)
			if initialOrigin.Kind != protocol.RecordOriginEnvironment || initialOrigin.ObjectID == "" || initialOrigin.Projection != "" {
				t.Fatal("initial environment has no full environment object")
			}
			intent, _, err := store.BeginCompaction(ctx, owner, IntentPayload{Kind: PendingIntentCompaction}, original.Context(), nil, providerAttemptTestPolicy())
			if err != nil {
				t.Fatal(err)
			}
			replacement, err := newNativeContext(tc.api)
			if err != nil {
				t.Fatal(err)
			}
			if err := replacement.AppendRecords(tc.records); err != nil {
				t.Fatal(err)
			}
			if signed, ok := replacement.(*anthropic.Context); ok {
				signed.PendingEffort = new("high")
			}
			if err := store.RetireLostExecutorSession(ctx, owner, "original-executor", ""); err != nil {
				t.Fatal(err)
			}
			if err := store.SetExecutorSession(ctx, owner, "replacement-executor", new("zsh")); err != nil {
				t.Fatal(err)
			}
			today = today.AddDate(0, 0, 1)
			audit := map[string]any{"response": "complete"}
			if tc.api == protocol.APIStyleChatCompletions {
				rejected := errors.New("compaction replacement rejected by provider policy")
				err := store.FinishCompaction(ctx, owner, intent, audit, original.Context(), replacement, func(candidate protocol.Context) error {
					records, err := candidate.Records()
					if err != nil || len(records) != len(tc.records)+1 {
						t.Fatalf("validation must see refreshed environment before pending notices: %s, %v", records, err)
					}
					checkEnvironment(records[tc.envIndex], "/fixture", "2026-01-03", "zsh")
					return rejected
				})
				if !errors.Is(err, rejected) {
					t.Fatalf("the replacement should be rejected: %v", err)
				}
				restored, err := store.Read(ctx, owner)
				if err != nil {
					t.Fatal(err)
				}
				if !sameNativeContext(restored.Context(), original.Context()) || restored.Pending == nil || restored.Pending.ID != intent {
					t.Fatalf("rejection must roll back the replacement and its intent settlement: %+v", restored.Pending)
				}
			}
			if err := store.FinishCompaction(ctx, owner, intent, audit, original.Context(), replacement, nil); err != nil {
				t.Fatal(err)
			}
			store = New(postgres.Open(t, dsn), store.operationTimeout, nil)
			store.contextSourceWrites = true
			store.now = func() time.Time { return today }
			// Read the persisted roots and check that an unchanged next step adds no
			// second environment message.
			prepared, err := store.PrepareSamplingContext(ctx, owner)
			if err != nil {
				t.Fatal(err)
			}
			got, err := prepared.Context().Records()
			if err != nil {
				t.Fatal(err)
			}
			if len(got) != len(tc.records)+3 || !strings.Contains(string(got[len(got)-2]), lostExecutorSessionNotice) ||
				!strings.Contains(string(got[len(got)-1]), `Active execution handles: {\"cells\":[],\"processes\":[]}`) {
				t.Fatalf("replacement did not contain the environment, executor-loss notice and empty compaction snapshot: %s", got)
			}
			if content := checkEnvironment(got[tc.envIndex], "/fixture", "2026-01-03", "zsh"); strings.Contains(content, "bash") {
				t.Fatalf("replacement environment contains the old shell: %s", content)
			}
			activeOrigin := prepared.Context().OriginAt(tc.envIndex)
			transcriptIndex := original.Transcript.RecordCount()
			transcriptOrigin := prepared.Transcript.OriginAt(transcriptIndex)
			if activeOrigin.Kind != protocol.RecordOriginEnvironment || activeOrigin.ObjectID == "" ||
				activeOrigin.ObjectID == initialOrigin.ObjectID || activeOrigin.Projection != "" ||
				transcriptOrigin.ObjectID != activeOrigin.ObjectID ||
				transcriptOrigin.Kind != activeOrigin.Kind || transcriptOrigin.Projection != protocol.RecordProjectionEnvironmentDiff {
				t.Fatal("full and diff projections did not share the same durable environment update")
			}
			object, err := store.ReadRecordObject(ctx, owner, false, tc.envIndex)
			if err != nil {
				t.Fatal(err)
			}
			environment := object.(EnvironmentUpdateObject)
			if environment.SessionID != session.ID || environment.Before == nil || environment.Before.CurrentDate != "2026-01-02" ||
				environment.Before.Shell == nil || *environment.Before.Shell != "bash" ||
				environment.After.Cwd != "/fixture" || environment.After.CurrentDate != "2026-01-03" ||
				environment.After.Shell == nil || *environment.After.Shell != "zsh" {
				t.Fatalf("compaction environment update lost its actual before/after state: %+v", environment)
			}
			for i, original := range tc.records {
				index := i
				if i >= tc.envIndex {
					index++
				}
				if !sameJSONValue(got[index], original) {
					t.Fatalf("compaction modified native record %d: got %s, want %s", i, got[index], original)
				}
			}
			if prepared.Transcript.RecordCount() != original.Transcript.RecordCount()+2 {
				t.Fatalf("transcript should gain one environment diff and one executor-loss notice")
			}
			// Ordinary replay keeps the replacement; only a real environment change
			// appends a new record, after the context already visible to the model.
			today = today.AddDate(0, 0, 1)
			update, err := store.PrepareSamplingContext(ctx, owner)
			if err != nil {
				t.Fatal(err)
			}
			updated, err := update.Context().Records()
			if err != nil || len(updated) != len(got)+1 {
				t.Fatalf("next environment update records=%s err=%v", updated, err)
			}
			checkEnvironment(updated[len(got)], "2026-01-04")
			dateOrigin := update.Context().OriginAt(len(got))
			if dateOrigin.Kind != protocol.RecordOriginEnvironment || dateOrigin.ObjectID == "" ||
				dateOrigin.ObjectID == activeOrigin.ObjectID || dateOrigin.Projection != protocol.RecordProjectionEnvironmentDiff {
				t.Fatal("the next environment update lost its origin")
			}
			object, err = store.ReadRecordObject(ctx, owner, true, transcriptIndex)
			if err != nil || object.(EnvironmentUpdateObject).After.CurrentDate != "2026-01-03" {
				t.Fatalf("later updates changed the historical environment object: object=%+v err=%v", object, err)
			}
			for i, record := range got {
				if !sameJSONValue(updated[i], record) {
					t.Fatalf("ordinary replay changed an existing record at index %d", i)
				}
			}
			if signed, ok := prepared.Context().(*anthropic.Context); ok {
				if signed.PendingEffort == nil || *signed.PendingEffort != "high" {
					t.Fatal("compaction environment consumed deferred effort")
				}
				if update.Context().(*anthropic.Context).PendingEffort == nil {
					t.Fatal("ordinary environment update consumed deferred effort")
				}
				admitInput(t, store, session.ID, "after", "resume the task")
				if _, err := store.NextCommand(ctx, owner, session.ID); err != nil {
					t.Fatal(err)
				}
				resumed, err := store.Read(ctx, owner)
				if err != nil {
					t.Fatal(err)
				}
				records, err := resumed.Context().Records()
				if err != nil {
					t.Fatal(err)
				}
				if len(records) != len(got)+3 || !strings.Contains(string(records[len(records)-2]), "resume the task") ||
					!strings.Contains(string(records[len(records)-1]), `"effort":"high"`) ||
					resumed.Context().(*anthropic.Context).PendingEffort != nil {
					t.Fatalf("user input did not restore deferred effort after itself: %s", records)
				}
			}
		})
	}
}
