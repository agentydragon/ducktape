package codex

import (
	"bytes"
	"encoding/json"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/runner/go/codex/protocol"
)

func TestDecodeCodexItemRetainsGeneratedVariants(t *testing.T) {
	for _, test := range []struct {
		typeName string
		want     reflect.Type
	}{
		{"agentMessage", reflect.TypeOf(protocol.AgentMessageThreadItem{})},
		{"reasoning", reflect.TypeOf(protocol.ReasoningThreadItem{})},
		{"commandExecution", reflect.TypeOf(protocol.CommandExecutionThreadItem{})},
		{"subAgentActivity", reflect.TypeOf(protocol.SubAgentActivityThreadItem{})},
	} {
		t.Run(test.typeName, func(t *testing.T) {
			item, err := decodeCodexItem(json.RawMessage(`{"type":"` + test.typeName + `","id":"item-1"}`))
			if err != nil {
				t.Fatal(err)
			}
			if got := reflect.TypeOf(item); got != test.want {
				t.Fatalf("decoded type = %v, want %v", got, test.want)
			}
		})
	}
}

func TestDecodeCodexItemPreservesUnknownRawPayload(t *testing.T) {
	raw := json.RawMessage(`{"type":"futureTool","id":"future-call","large":9007199254740993,"explicitNull":null}`)
	item, err := decodeCodexItem(raw)
	if err != nil {
		t.Fatal(err)
	}
	unknown, ok := item.(codexUnknownItem)
	if !ok {
		t.Fatalf("decoded type = %T, want codexUnknownItem", item)
	}
	if unknown.typeName != "futureTool" || unknown.id != "future-call" {
		t.Fatalf("unknown item identity = (%q, %q)", unknown.typeName, unknown.id)
	}
	if !bytes.Equal(unknown.raw, raw) {
		t.Fatalf("raw item changed: got %s, want %s", unknown.raw, raw)
	}
	fields, err := unknown.fields()
	if err != nil {
		t.Fatal(err)
	}
	if string(fields["large"]) != "9007199254740993" {
		t.Fatalf("large integer changed: %s", fields["large"])
	}
	if string(fields["explicitNull"]) != "null" {
		t.Fatalf("explicit null changed: %s", fields["explicitNull"])
	}
}
