package testing

import (
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionconfig"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/testsupport"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
)

const fixturePlatformInstructions = "Fixture Agentplane platform guidance."

func OpenStoreSession(t *testing.T, store *sessionstore.Store, request *sessionconfig.Configuration, ownerUID string, api protocol.APIStyle) (*sessionstore.Session, error) {
	t.Helper()
	testsupport.FillSessionConfigurationDefaults(request)
	platformInstructions := fixturePlatformInstructions
	return store.CreateSession(t.Context(), request, ownerUID, api, &platformInstructions)
}

func OpenSessionForStore(t *testing.T, store *sessionstore.Store, setup string) *sessionstore.Session {
	t.Helper()
	request := &sessionconfig.Configuration{
		SessionId:   uuid.NewString(),
		Spec:        &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"},
		SetupScript: &setup,
	}
	session, err := OpenStoreSession(t, store, request, "fixture-account", protocol.APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	return session
}

func AssertNoUnresolvedNestedIntents(t *testing.T, store *sessionstore.Store, owner sessionstore.Owner) {
	t.Helper()
	var count int
	if err := store.DB().QueryRowContext(t.Context(), "SELECT count(*) FROM sampler_intent WHERE conversation_id=$1 AND result IS NULL", owner.ConversationID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("unresolved nested intents=%d", count)
	}
}

func AdmitInput(t *testing.T, store *sessionstore.Store, sessionID, commandID, text string) {
	t.Helper()
	if _, err := store.Admit(t.Context(), sessionID, &commandpb.Command{CommandId: commandID, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: text}}}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
}

func AppendModelToolCall(t *testing.T, store *sessionstore.Store, owner sessionstore.Owner, call protocol.ToolInvocation) {
	t.Helper()
	intentID, _, err := store.Begin(t.Context(), owner, sessionstore.IntentPayload{Kind: sessionstore.PendingIntentSampling}, nil, &sessionstore.ProviderAttemptPolicy{Budget: 3, Lease: time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	itemType := responses.ItemFunctionCall
	if call.Encoding == protocol.ExecEncodingFreeform {
		itemType = responses.ItemCustomToolCall
	}
	item := responses.Item{Type: itemType, CallID: call.CallID, Name: call.Name}
	if call.Encoding == protocol.ExecEncodingJSON {
		item.Arguments = call.Input
	} else {
		item.Input = call.Input
	}
	if err := store.Finish(t.Context(), owner, intentID, map[string]any{"output": []responses.Item{item}}, testsupport.NativeResponsesRecords(t, item), nil); err != nil {
		t.Fatal(err)
	}
}
