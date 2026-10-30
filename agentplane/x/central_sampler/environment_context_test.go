package centralsampler

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/responses"
)

// The expected strings are Codex rust-v0.162.1's rendered `<environment_context>` shape, which the
// model is trained on.
func TestEnvironmentContextUpdate(t *testing.T) {
	bash, zsh := "bash", "zsh"
	baseline := &EnvironmentContext{Cwd: "/fixture", Shell: &bash, CurrentDate: "2026-01-02"}
	for name, test := range map[string]struct {
		previous *EnvironmentContext
		current  EnvironmentContext
		want     string
	}{
		"first render is complete": {
			current: EnvironmentContext{Cwd: "/fixture", Shell: &bash, CurrentDate: "2026-01-02"},
			want:    "<environment_context>\n  <cwd>/fixture</cwd>\n  <shell>bash</shell>\n  <current_date>2026-01-02</current_date>\n</environment_context>",
		},
		"unchanged renders nothing": {
			previous: baseline,
			current:  EnvironmentContext{Cwd: "/fixture", Shell: &bash, CurrentDate: "2026-01-02"},
		},
		"date rollover renders only the date": {
			previous: baseline,
			current:  EnvironmentContext{Cwd: "/fixture", Shell: &bash, CurrentDate: "2026-01-03"},
			want:     "<environment_context>\n  <current_date>2026-01-03</current_date>\n</environment_context>",
		},
		"changed shell renders the environment": {
			previous: baseline,
			current:  EnvironmentContext{Cwd: "/fixture", Shell: &zsh, CurrentDate: "2026-01-02"},
			want:     "<environment_context>\n  <cwd>/fixture</cwd>\n  <shell>zsh</shell>\n  <current_date>2026-01-02</current_date>\n</environment_context>",
		},
		"first learned shell renders the environment": {
			previous: &EnvironmentContext{Cwd: "/fixture", CurrentDate: "2026-01-02"},
			current:  EnvironmentContext{Cwd: "/fixture", Shell: &bash, CurrentDate: "2026-01-02"},
			want:     "<environment_context>\n  <cwd>/fixture</cwd>\n  <shell>bash</shell>\n  <current_date>2026-01-02</current_date>\n</environment_context>",
		},
		"lost shell renders nothing": {
			previous: baseline,
			current:  EnvironmentContext{Cwd: "/fixture", CurrentDate: "2026-01-02"},
		},
		"values are XML-escaped": {
			current: EnvironmentContext{Cwd: `/a&b<c>"d'`, CurrentDate: "2026-01-02"},
			want:    "<environment_context>\n  <cwd>/a&amp;b&lt;c&gt;&quot;d&apos;</cwd>\n  <current_date>2026-01-02</current_date>\n</environment_context>",
		},
	} {
		t.Run(name, func(t *testing.T) {
			if got := environmentContextUpdate(test.previous, test.current); got != test.want {
				t.Fatalf("update=%q, want %q", got, test.want)
			}
		})
	}
}

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
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	submit := func(id, text string) []responses.Item {
		t.Helper()
		if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
			CommandId: id, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: text}},
		}, "fixture-account", nil); err != nil {
			t.Fatal(err)
		}
		if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
			t.Fatal(err)
		}
		conversation, err := store.Read(t.Context(), owner)
		if err != nil {
			t.Fatal(err)
		}
		return responsesItems(t, conversation.Context())
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
	conversation, err := store.PrepareSamplingContext(t.Context(), owner, &ExecutionState{})
	if err != nil {
		t.Fatal(err)
	}
	items = responsesItems(t, conversation.Context())
	if got := messageText(t, items[len(items)-1]); !strings.Contains(got, "<shell>bash</shell>") {
		t.Fatalf("learned shell was not rendered before sampling: %q", got)
	}
	unchanged, err := store.PrepareSamplingContext(t.Context(), owner, &ExecutionState{})
	if err != nil {
		t.Fatal(err)
	}
	if got := len(responsesItems(t, unchanged.Context())); got != len(items) {
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

func TestCompactionReinjectsFullEnvironmentContextBeforeCompactionItem(t *testing.T) {
	store := newTestStore(t)
	store.now = func() time.Time { return time.Date(2026, 1, 2, 12, 0, 0, 0, time.Local) }
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	bash := "bash"
	if err := store.SetExecutorSession(t.Context(), owner, "fixture-executor-session", &bash); err != nil {
		t.Fatal(err)
	}
	conversation, err := store.PrepareSamplingContext(t.Context(), owner, &ExecutionState{})
	if err != nil {
		t.Fatal(err)
	}
	intentID, err := store.BeginCompaction(t.Context(), owner, IntentRequest{Kind: PendingIntentCompaction}, conversation.Context())
	if err != nil {
		t.Fatal(err)
	}
	originalItems := responsesItems(t, conversation.Context())
	replacement := &responses.Context{Items: []responses.Item{
		originalItems[len(originalItems)-1],
		{Type: responses.ItemCompaction, EncryptedContent: "fixture-opaque"},
	}}
	if err := store.FinishCompaction(t.Context(), owner, intentID, map[string]any{"response": "complete"}, conversation.Context(), replacement); err != nil {
		t.Fatal(err)
	}
	compacted, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	items := responsesItems(t, compacted.Context())
	if len(items) != 2 || items[1].EncryptedContent != "fixture-opaque" {
		t.Fatalf("compacted context=%+v", items)
	}
	want := "<environment_context>\n  <cwd>/fixture</cwd>\n  <shell>bash</shell>\n  <current_date>2026-01-02</current_date>\n</environment_context>"
	if got := messageText(t, items[0]); got != want {
		t.Fatalf("reinjected context=%q, want %q", got, want)
	}
}
