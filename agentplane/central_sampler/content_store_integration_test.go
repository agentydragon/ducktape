package centralsampler

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/storage"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
)

const storedRequestInstruction = "Keep the fixture command safe and concise."

func openContentStoreSession(t *testing.T, store *Store, account string) (*Session, Owner) {
	t.Helper()
	request := &samplerpb.CreateSessionRequest{
		SessionId: uuid.NewString(),
		Spec:      &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"},
	}
	session, err := openStoreSession(t, store, request, account, protocol.APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	return session, owner
}

func responsesRequestForTranscript(t *testing.T, store *Store, owner Owner, instructions string, tools []responses.AdditionalTool, metadata *responses.ClientMetadata) responses.Request {
	t.Helper()
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	transcript, ok := conversation.Transcript.(*responses.Context)
	if !ok {
		t.Fatalf("transcript is %T, want *responses.Context", conversation.Transcript)
	}
	records, err := transcript.Records()
	if err != nil {
		t.Fatal(err)
	}
	input := make([]responses.RequestInput, len(records))
	for index, raw := range records {
		var item responses.Item
		if err := json.Unmarshal(raw, &item); err != nil {
			t.Fatal(err)
		}
		input[index] = item
	}
	return responses.Request{
		Model:          "fixture-provider-model",
		Instructions:   instructions,
		Input:          input,
		Stream:         true,
		ClientMetadata: metadata,
		Tools:          tools,
	}
}

func dispatchStoredResponsesRequest(t *testing.T, store *Store, owner Owner, request responses.Request) (string, []byte) {
	t.Helper()
	connection := ConnectionConfig{BaseURL: "https://provider.example.test"}
	config := ModelConfig{
		Connection:    "fixture-connection",
		ProviderModel: "fixture-provider-model",
		API:           protocol.APIStyleResponses,
		Responses:     responses.Config{Dialect: responses.DialectLite},
	}
	intent := providerRequestIntent(PendingIntentSampling, connection, config)
	requestID, wire, err := store.Begin(t.Context(), owner, intent, fixtureResponsesPreparation(request))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Finish(t.Context(), owner, requestID, map[string]any{"status": "ok"}, nil, nil); err != nil {
		t.Fatal(err)
	}
	return requestID, wire
}

func fixtureResponsesTool(name, description string, parameters string) []responses.AdditionalTool {
	return []responses.AdditionalTool{{
		Type:        "function",
		Name:        name,
		Description: description,
		Parameters:  json.RawMessage(parameters),
	}}
}

func contentRowCount(t *testing.T, store *Store, kind storage.ContentKind) int {
	t.Helper()
	var count int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT COUNT(*) FROM sampler_content WHERE kind=$1`, string(kind)).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func tableRowCount(t *testing.T, store *Store, table string) int {
	t.Helper()
	// The only callers pass fixed table names in this test file.
	var count int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT COUNT(*) FROM `+table).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func equivalentJSON(t *testing.T, got, want []byte) {
	t.Helper()
	decode := func(raw []byte) any {
		t.Helper()
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.UseNumber()
		var value any
		if err := decoder.Decode(&value); err != nil {
			t.Fatalf("decode JSON %q: %v", raw, err)
		}
		return value
	}
	if left, right := decode(got), decode(want); !reflect.DeepEqual(left, right) {
		gotJSON, _ := json.Marshal(left)
		wantJSON, _ := json.Marshal(right)
		t.Fatalf("JSON differs:\n got: %s\nwant: %s", gotJSON, wantJSON)
	}
}

func assertDispatchBoundaryUsesContentReferences(t *testing.T, store *Store, sessionID, requestID, prompt string) {
	t.Helper()
	entries, err := store.Events(t.Context(), sessionID, 0, 4096)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		boundary := entry.GetEvent().GetBoundaryRecord()
		if boundary == nil || boundary.GetCorrelationId() != requestID || boundary.GetBoundary() != "sampling" || boundary.GetDirection() != "dispatch" {
			continue
		}
		var payload map[string]json.RawMessage
		if err := json.Unmarshal(boundary.GetPayload(), &payload); err != nil {
			t.Fatalf("decode sampling dispatch boundary: %v", err)
		}
		if string(payload["request_id"]) != string(protocol.MustJSON(requestID)) {
			t.Fatalf("dispatch omitted request ID: %s", boundary.GetPayload())
		}
		for _, key := range []string{"context_id", "profile_id", "source_context_id", "prefix_ids", "suffix_ids", "body", "wire_request"} {
			if _, present := payload[key]; present {
				t.Fatalf("dispatch exposed private storage field %q", key)
			}
		}
		if strings.Contains(string(boundary.GetPayload()), prompt) {
			t.Fatal("sampling dispatch journal duplicated the full user prompt")
		}
		return
	}
	t.Fatalf("no sampling dispatch BoundaryRecord for request %s", requestID)
}

func TestProviderRequestStorageHasLinearContextGrowthAndFrozenProfile(t *testing.T) {
	store := newTestStore(t)
	session, owner := openContentStoreSession(t, store, "fixture-account")
	const turns = 30
	baseTools := fixtureResponsesTool("exec", "Run a command.", `{"type":"object","properties":{"command":{"type":"string"}},"required":["command"],"additionalProperties":false}`)
	var firstID, duplicateID, lastID string
	var firstWire, duplicateWire, lastWire []byte
	var duplicateRequest responses.Request

	for turn := 0; turn < turns; turn++ {
		prompt := fmt.Sprintf("Unique persistent-context prompt %02d.", turn)
		if err := store.AppendInput(t.Context(), owner, prompt); err != nil {
			t.Fatalf("append prompt %d: %v", turn, err)
		}
		instructions := fmt.Sprintf("Caller attempt %d to replace frozen instructions.", turn)
		tools := fixtureResponsesTool(fmt.Sprintf("changed_exec_%02d", turn), fmt.Sprintf("Changed description %d.", turn), `{"type":"object","properties":{"different":{"type":"number"}}}`)
		if turn == 0 {
			instructions = storedRequestInstruction
			tools = baseTools
		}
		metadata := &responses.ClientMetadata{SessionID: "fixture-session", ThreadID: "fixture-thread", TurnID: fmt.Sprintf("turn-%02d", turn)}
		request := responsesRequestForTranscript(t, store, owner, instructions, tools, metadata)
		requestID, wire := dispatchStoredResponsesRequest(t, store, owner, request)
		if turn == 0 {
			firstID, firstWire, duplicateRequest = requestID, wire, request
			equivalentJSON(t, wire, protocol.MustJSON(request))
		}
		if turn == turns-1 {
			lastID, lastWire = requestID, wire
		}
	}

	// Re-dispatch the exact first request after it has completed. The body is stable while each
	// durable intent receives its own distinct authorization handle.
	duplicateID, duplicateWire = dispatchStoredResponsesRequest(t, store, owner, duplicateRequest)
	if duplicateID == firstID {
		t.Fatalf("identical requests reused intent ID %s", firstID)
	}
	equivalentJSON(t, duplicateWire, firstWire)

	first, err := store.LoadProviderRequest(t.Context(), session.ID, session.AccountUID, firstID)
	if err != nil {
		t.Fatalf("load original provider request: %v", err)
	}
	equivalentJSON(t, first.Body, firstWire)
	duplicate, err := store.LoadProviderRequest(t.Context(), session.ID, session.AccountUID, duplicateID)
	if err != nil {
		t.Fatalf("load duplicate provider request: %v", err)
	}
	equivalentJSON(t, duplicate.Body, firstWire)
	last, err := store.LoadProviderRequest(t.Context(), session.ID, session.AccountUID, lastID)
	if err != nil {
		t.Fatalf("load last provider request: %v", err)
	}
	equivalentJSON(t, last.Body, lastWire)
	var restored struct {
		Instructions string          `json:"instructions"`
		Tools        json.RawMessage `json:"tools"`
	}
	if err := json.Unmarshal(last.Body, &restored); err != nil {
		t.Fatal(err)
	}
	if restored.Instructions != storedRequestInstruction {
		t.Fatalf("restored last request lost frozen profile: instructions=%q tools=%+v", restored.Instructions, restored.Tools)
	}
	equivalentJSON(t, restored.Tools, protocol.MustJSON(baseTools))

	if got := tableRowCount(t, store, "sampler_context"); got != turns {
		t.Fatalf("sampler_context rows=%d after %d append-only inputs, want linear count %d", got, turns, turns)
	}
	if got := contentRowCount(t, store, responses.ContentOpenAIItem); got != turns {
		t.Fatalf("item content rows=%d, want %d complete unique prompt items", got, turns)
	}
	if got := contentRowCount(t, store, storage.ContentText); got != 1 {
		t.Fatalf("instruction content rows=%d, want one shared instruction", got)
	}
	if got := contentRowCount(t, store, responses.ContentOpenAITool); got != 1 {
		t.Fatalf("OpenAI tool content rows=%d, want one frozen shared definition", got)
	}
	assertDispatchBoundaryUsesContentReferences(t, store, session.ID, firstID, "Unique persistent-context prompt 00.")
}

func TestProviderRequestContentSharesAcrossAccountsAndReadsAreAuthorized(t *testing.T) {
	store := newTestStore(t)
	firstSession, firstOwner := openContentStoreSession(t, store, "fixture-account")
	secondSession, secondOwner := openContentStoreSession(t, store, "other-fixture-account")
	tool := fixtureResponsesTool("shared_exec", "Shared tool definition.", `{"type":"object","properties":{"command":{"type":"string"}},"required":["command"],"additionalProperties":false}`)

	if err := store.AppendInput(t.Context(), firstOwner, "A prompt shared across accounts."); err != nil {
		t.Fatal(err)
	}
	metadata := (*responses.ClientMetadata)(nil)
	firstRequest := responsesRequestForTranscript(t, store, firstOwner, storedRequestInstruction, tool, metadata)
	firstID, firstWire := dispatchStoredResponsesRequest(t, store, firstOwner, firstRequest)
	firstContentRows := tableRowCount(t, store, "sampler_content")
	firstContextRows := tableRowCount(t, store, "sampler_context")

	if err := store.AppendInput(t.Context(), secondOwner, "A prompt shared across accounts."); err != nil {
		t.Fatal(err)
	}
	secondRequest := responsesRequestForTranscript(t, store, secondOwner, storedRequestInstruction, tool, metadata)
	secondID, secondWire := dispatchStoredResponsesRequest(t, store, secondOwner, secondRequest)
	if firstID == secondID {
		t.Fatalf("separate sessions reused request ID %s", firstID)
	}
	equivalentJSON(t, secondWire, firstWire)
	if got := tableRowCount(t, store, "sampler_content"); got != firstContentRows {
		t.Fatalf("identical cross-account request added content rows: before=%d after=%d", firstContentRows, got)
	}
	if got := tableRowCount(t, store, "sampler_context"); got != firstContextRows {
		t.Fatalf("identical cross-account input added context rows: before=%d after=%d", firstContextRows, got)
	}
	if got := contentRowCount(t, store, responses.ContentOpenAITool); got != 1 {
		t.Fatalf("shared tool content rows=%d, want one", got)
	}

	first, err := store.LoadProviderRequest(t.Context(), firstSession.ID, firstSession.AccountUID, firstID)
	if err != nil {
		t.Fatalf("authorized request read failed: %v", err)
	}
	second, err := store.LoadProviderRequest(t.Context(), secondSession.ID, secondSession.AccountUID, secondID)
	if err != nil {
		t.Fatalf("second authorized request read failed: %v", err)
	}
	if first.ProfileID != second.ProfileID {
		t.Fatalf("identical provider profiles did not share a content ID: %s != %s", first.ProfileID, second.ProfileID)
	}
	equivalentJSON(t, first.Body, firstWire)
	equivalentJSON(t, second.Body, firstWire)
	if _, err := store.LoadProviderRequest(t.Context(), firstSession.ID, secondSession.AccountUID, firstID); !errors.Is(err, protocol.ErrMissing) {
		t.Fatalf("foreign account request read error=%v, want ErrMissing", err)
	}
	if _, err := store.LoadProviderRequest(t.Context(), secondSession.ID, secondSession.AccountUID, firstID); !errors.Is(err, protocol.ErrMissing) {
		t.Fatalf("foreign session request read error=%v, want ErrMissing", err)
	}
}

func fixtureResponsesPreparation(body responses.Request) requestPreparation {
	return func(store *contentStore, conversation *Conversation, metadata ProviderRequest) (ProviderRequest, []byte, error) {
		previous, err := store.previousRequest(conversation.ID)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		if previous.ProfileID != "" {
			body, err = responses.FreezeRequest(store, previous.ProfileID, body)
			if err != nil {
				return ProviderRequest{}, nil, err
			}
		}
		profile, ids, err := responses.StoreRequest(store, body)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		return finishStoredRequest(store, metadata, profile, ids)
	}
}

func responseContextForTest(t *testing.T, items []responses.Item) *responses.Context {
	t.Helper()
	state := &responses.Context{}
	records := make([]json.RawMessage, 0, len(items))
	for _, item := range items {
		records = append(records, protocol.MustJSON(item))
	}
	if err := state.AppendRecords(records); err != nil {
		t.Fatal(err)
	}
	return state
}
