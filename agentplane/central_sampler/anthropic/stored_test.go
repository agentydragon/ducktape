package anthropic

import (
	"bytes"
	"encoding/json"
	"fmt"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

type memoryContentStore struct {
	contents map[protocol.ContentID]protocol.StoredContent
}

func newMemoryContentStore() *memoryContentStore {
	return &memoryContentStore{contents: make(map[protocol.ContentID]protocol.StoredContent)}
}

func (store *memoryContentStore) PutContent(kind protocol.ContentKind, payload json.RawMessage) (protocol.ContentID, error) {
	content, err := protocol.CanonicalContent(kind, payload)
	if err != nil {
		return "", err
	}
	if previous, ok := store.contents[content.ID]; ok && !bytes.Equal(previous.Payload, content.Payload) {
		return "", fmt.Errorf("hash collision for %s", content.ID)
	}
	store.contents[content.ID] = content
	return content.ID, nil
}

func (store *memoryContentStore) GetContent(id protocol.ContentID) (protocol.StoredContent, error) {
	content, ok := store.contents[id]
	if !ok {
		return protocol.StoredContent{}, fmt.Errorf("missing content %s", id)
	}
	content.Payload = append(json.RawMessage(nil), content.Payload...)
	return content, nil
}

func TestStoreRecordRoundTripsAnthropicNativeBlocksAndDeduplicatesPayloads(t *testing.T) {
	record := json.RawMessage(`{
		"id":"msg_native",
		"type":"message",
		"role":"assistant",
		"model":"claude-test",
		"content":[
			{"type":"text","text":"shared text\n"},
			{"type":"thinking","thinking":"shared text\n","signature":"thinking-signature","unknown_block_field":{"exact":900719925474099312345}},
			{"type":"redacted_thinking","data":"opaque continuation","signature":"redacted-signature"},
			{"type":"tool_use","id":"call_1","name":"exec","input":{"precision":900719925474099312345}},
			{"type":"tool_result","tool_use_id":"call_1","content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"aGVsbG8=","unknown_source":{"flag":true}}}]},
			{"type":"image","source":{"type":"base64","media_type":"image/png","data":"aGVsbG8=","unknown_source":{"flag":true}}},
			{"type":"compaction","content":"compact summary","signature":"compaction-signature"}
		],
		"stop_reason":"end_turn",
		"usage":{"input_tokens":900719925474099312345,"output_tokens":7,"unknown_usage":{"count":900719925474099312345}},
		"unknown_envelope":{"keep":"metadata"}
	}`)
	store := newMemoryContentStore()
	id, err := StoreRecord(store, record)
	if err != nil {
		t.Fatal(err)
	}
	message, err := store.GetContent(id)
	if err != nil {
		t.Fatal(err)
	}
	var storedMessage messageContent
	if err := json.Unmarshal(message.Payload, &storedMessage); err != nil {
		t.Fatal(err)
	}
	if len(storedMessage.Blocks) != 7 {
		t.Fatalf("stored %d blocks, want 7", len(storedMessage.Blocks))
	}
	var textID, thinkingID protocol.ContentID
	for _, blockID := range storedMessage.Blocks {
		block, err := store.GetContent(blockID)
		if err != nil {
			t.Fatal(err)
		}
		var content blockContent
		if err := json.Unmarshal(block.Payload, &content); err != nil {
			t.Fatal(err)
		}
		switch content.Type {
		case BlockText:
			textID = content.ScalarID
		case BlockThinking:
			thinkingID = content.ScalarID
		}
	}
	if textID == "" || textID != thinkingID {
		t.Fatalf("text/thinking payloads did not share ContentText row: text=%s thinking=%s", textID, thinkingID)
	}
	imageDataCount, opaqueCount := 0, 0
	for _, content := range store.contents {
		switch content.Kind {
		case protocol.ContentImageData:
			imageDataCount++
		case protocol.ContentOpaque:
			opaqueCount++
		}
	}
	if imageDataCount != 1 {
		t.Fatalf("stored %d image payload rows, want one shared row", imageDataCount)
	}
	if opaqueCount != 1 {
		t.Fatalf("stored %d opaque continuation rows, want one", opaqueCount)
	}

	restored, err := RestoreRecord(store, id)
	if err != nil {
		t.Fatal(err)
	}
	assertJSONEqual(t, record, restored)
}

func TestStoreRequestSeparatesReusableProfileAndOrderedMessages(t *testing.T) {
	store := newMemoryContentStore()
	messageA := json.RawMessage(`{"role":"user","content":[{"type":"text","text":"first"}]}`)
	messageB := json.RawMessage(`{"role":"assistant","content":[{"type":"thinking","thinking":"signed prefix","signature":"opaque-sig"},{"type":"text","text":"answer"}]}`)
	tool := Tool{
		Name:                "exec",
		Description:         "Execute code.",
		Strict:              true,
		EagerInputStreaming: true,
		InputSchema:         toolInputSchema{Type: "object", Properties: map[string]toolInputSchema{"code": {Type: "string"}}},
	}
	request := Request{
		CacheControl: &CacheControl{Type: "ephemeral", TTL: "1h"},
		Diagnostics:  &Diagnostics{PreviousMessageID: stringPointer("msg_previous")},
		MaxTokens:    1024,
		Messages:     []json.RawMessage{messageA, messageB},
		Model:        "claude-test",
		OutputConfig: &OutputConfig{Effort: "high"},
		Stream:       true,
		System:       "shared system prompt",
		Tools:        []Tool{tool},
	}
	profileA, recordsA, err := StoreRequest(store, request)
	if err != nil {
		t.Fatal(err)
	}
	wantRequestA, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	request.Diagnostics = &Diagnostics{PreviousMessageID: stringPointer("msg_newer")}
	profileB, recordsB, err := StoreRequest(store, request)
	if err != nil {
		t.Fatal(err)
	}
	if profileA == profileB {
		t.Fatal("per-call diagnostics did not distinguish request profiles")
	}
	profileContent, err := store.GetContent(profileA)
	if err != nil {
		t.Fatal(err)
	}
	var profile requestContent
	if err := json.Unmarshal(profileContent.Payload, &profile); err != nil {
		t.Fatal(err)
	}
	otherProfileContent, err := store.GetContent(profileB)
	if err != nil {
		t.Fatal(err)
	}
	var otherProfile requestContent
	if err := json.Unmarshal(otherProfileContent.Payload, &otherProfile); err != nil {
		t.Fatal(err)
	}
	if profile.SystemID != otherProfile.SystemID {
		t.Fatalf("system prompt was not shared: %s != %s", profile.SystemID, otherProfile.SystemID)
	}
	if !reflect.DeepEqual(profile.Tools, otherProfile.Tools) {
		t.Fatalf("tool rows were not shared: %v != %v", profile.Tools, otherProfile.Tools)
	}
	if !reflect.DeepEqual(recordsA, recordsB) || len(recordsA) != 2 {
		t.Fatalf("message references lost order or changed: %v != %v", recordsA, recordsB)
	}
	messageJSON := make([]json.RawMessage, 0, len(recordsA))
	for _, recordID := range recordsA {
		restored, err := RestoreRecord(store, recordID)
		if err != nil {
			t.Fatal(err)
		}
		messageJSON = append(messageJSON, restored)
	}
	restored, err := RestoreRequest(store, profileA, messageJSON)
	if err != nil {
		t.Fatal(err)
	}
	assertJSONEqual(t, wantRequestA, restored)
}

func TestStoreRequestJSONPreservesHistoricalUnknownFields(t *testing.T) {
	store := newMemoryContentStore()
	historical := json.RawMessage(`{
		"model":"claude-old",
		"max_tokens":900719925474099312345,
		"stream":true,
		"system":"shared legacy prompt",
		"tools":[{"name":"exec","vendor_tool_metadata":{"precision":900719925474099312345}}],
		"messages":[{"role":"assistant","content":[{"type":"thinking","thinking":"signed","signature":"opaque"}]}],
		"future_request_field":{"future_number":900719925474099312345}
	}`)
	profile, messageIDs, err := StoreRequestJSON(store, historical)
	if err != nil {
		t.Fatal(err)
	}
	messages := make([]json.RawMessage, 0, len(messageIDs))
	for _, messageID := range messageIDs {
		message, err := RestoreRecord(store, messageID)
		if err != nil {
			t.Fatal(err)
		}
		messages = append(messages, message)
	}
	restored, err := RestoreRequest(store, profile, messages)
	if err != nil {
		t.Fatal(err)
	}
	assertJSONEqual(t, historical, restored)
}

func TestRestoreRecordChecksReferencedContent(t *testing.T) {
	store := newMemoryContentStore()
	messageID, err := StoreRecord(store, json.RawMessage(`{"role":"assistant","content":[{"type":"text","text":"payload"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	message, err := store.GetContent(messageID)
	if err != nil {
		t.Fatal(err)
	}
	var content messageContent
	if err := json.Unmarshal(message.Payload, &content); err != nil {
		t.Fatal(err)
	}
	block, err := store.GetContent(content.Blocks[0])
	if err != nil {
		t.Fatal(err)
	}
	var blockData blockContent
	if err := json.Unmarshal(block.Payload, &blockData); err != nil {
		t.Fatal(err)
	}
	textContent, ok := store.contents[blockData.ScalarID]
	if !ok {
		t.Fatalf("missing text payload %s", blockData.ScalarID)
	}
	delete(store.contents, blockData.ScalarID)
	if _, err := RestoreRecord(store, messageID); err == nil {
		t.Fatal("RestoreRecord accepted a missing text dependency")
	}
	store.contents[blockData.ScalarID] = textContent
	textContent.Payload = json.RawMessage(`"tampered"`)
	store.contents[blockData.ScalarID] = textContent
	if _, err := RestoreRecord(store, messageID); err == nil {
		t.Fatal("RestoreRecord accepted a content hash mismatch")
	}
}

func assertJSONEqual(t *testing.T, want, got []byte) {
	t.Helper()
	var wantValue any
	decoder := json.NewDecoder(bytes.NewReader(want))
	decoder.UseNumber()
	if err := decoder.Decode(&wantValue); err != nil {
		t.Fatalf("decode expected JSON: %v", err)
	}
	var gotValue any
	decoder = json.NewDecoder(bytes.NewReader(got))
	decoder.UseNumber()
	if err := decoder.Decode(&gotValue); err != nil {
		t.Fatalf("decode actual JSON: %v", err)
	}
	if !reflect.DeepEqual(wantValue, gotValue) {
		t.Fatalf("JSON differs:\n got: %s\nwant: %s", got, want)
	}
}

func stringPointer(value string) *string { return &value }
