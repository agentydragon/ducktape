package providerrequest

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
)

type memoryContent map[content.ContentID]content.StoredContent

func (m memoryContent) PutContent(kind content.ContentKind, payload json.RawMessage) (content.ContentID, error) {
	stored, err := content.CanonicalContent(kind, payload)
	if err != nil {
		return "", err
	}
	m[stored.ID] = stored
	return stored.ID, nil
}

func (m memoryContent) GetContent(id content.ContentID) (content.StoredContent, error) {
	stored, ok := m[id]
	if !ok {
		return content.StoredContent{}, fmt.Errorf("missing fixture content %s", id)
	}
	return stored, content.VerifyContent(stored)
}

func TestPreparationFreezesAuthoredPrefixAcrossRequests(t *testing.T) {
	configs := []samplersettings.ModelConfig{
		{API: protocol.APIStyleChatCompletions, ProviderModel: "fixture-model", ExecEncoding: protocol.ExecEncodingJSON},
		{API: protocol.APIStyleResponses, ProviderModel: "fixture-model", ExecEncoding: protocol.ExecEncodingJSON, Responses: responses.Config{Dialect: responses.DialectLite}},
		{API: protocol.APIStyleResponses, ProviderModel: "fixture-model", ExecEncoding: protocol.ExecEncodingJSON, Responses: responses.Config{Dialect: responses.DialectStandard}},
		{API: protocol.APIStyleAnthropicMessages, ProviderModel: "fixture-model", ExecEncoding: protocol.ExecEncodingJSON, AnthropicMessages: &anthropic.Config{MaxTokens: 1024}},
	}
	for _, config := range configs {
		t.Run(string(config.API)+"/"+string(config.Responses.Dialect), func(t *testing.T) {
			memory := memoryContent{}
			firstInstructions := "fixture original authored prefix"
			snapshot := Snapshot{API: config.API, Instructions: &firstInstructions}
			preparation := Preparation{ModelConfig: config}
			first, err := Prepare(memory, snapshot, preparation)
			if err != nil {
				t.Fatal(err)
			}
			if projection, ok := first.History.(Projection); !ok || projection.Key != ProjectionFor(config) {
				t.Fatalf("history = %#v", first.History)
			}
			if config.API == protocol.APIStyleResponses {
				// Emulate a retained profile authored by an earlier template version.
				wire, err := Render(memory, config.API, first.ProfileID, nil)
				if err != nil {
					t.Fatal(err)
				}
				var retained responses.Request
				if err := json.Unmarshal(wire, &retained); err != nil {
					t.Fatal(err)
				}
				retained.Instructions = firstInstructions
				first.ProfileID, _, err = responses.StoreRequest(memory, retained)
				if err != nil {
					t.Fatal(err)
				}
			}
			changed := "fixture changed authored prefix"
			snapshot.Instructions, snapshot.PreviousProfileID = &changed, first.ProfileID
			if len(first.PrefixIDs) > 0 {
				snapshot.PreviousFirstID = first.PrefixIDs[0]
			}
			next, err := Prepare(memory, snapshot, preparation)
			if err != nil {
				t.Fatal(err)
			}
			wire, err := Render(memory, config.API, next.ProfileID, next.PrefixIDs)
			if err != nil {
				t.Fatal(err)
			}
			if config.API == protocol.APIStyleResponses {
				var restored struct {
					Instructions string `json:"instructions"`
				}
				if err := json.Unmarshal(wire, &restored); err != nil {
					t.Fatal(err)
				}
				if restored.Instructions != firstInstructions {
					t.Fatal("retained template instructions changed")
				}
				// Session instructions remain a per-request developer message.
				if !strings.Contains(string(wire), changed) {
					t.Fatal("current Session instructions missing from input")
				}
			} else if !strings.Contains(string(wire), firstInstructions) || strings.Contains(string(wire), changed) {
				t.Fatal("retained system instructions changed")
			}
			if first.ProfileID != next.ProfileID {
				t.Fatal("unchanged profile received another content hash")
			}
		})
	}
}

func TestAnthropicCompactionCarriesCompleteNativeContext(t *testing.T) {
	memory := memoryContent{}
	native := json.RawMessage(`{"role":"user","content":[{"type":"text","text":"fixture compacted context"}]}`)
	body := &anthropic.Request{Model: "fixture-model", MaxTokens: 1024, System: "fixture system", Messages: []json.RawMessage{native}}
	recipe, err := Prepare(memory, Snapshot{API: protocol.APIStyleAnthropicMessages}, Preparation{
		ModelConfig: samplersettings.ModelConfig{API: protocol.APIStyleAnthropicMessages}, Compaction: true, AnthropicCompactionRequest: body,
	})
	if err != nil {
		t.Fatal(err)
	}
	complete, ok := recipe.History.(Complete)
	if !ok || len(complete.IDs) != 1 {
		t.Fatalf("history = %#v", recipe.History)
	}
	wire, err := Render(memory, protocol.APIStyleAnthropicMessages, recipe.ProfileID, complete.IDs)
	if err != nil {
		t.Fatal(err)
	}
	var restored anthropic.Request
	if err := json.Unmarshal(wire, &restored); err != nil {
		t.Fatal(err)
	}
	if len(restored.Messages) != 1 || !strings.Contains(string(restored.Messages[0]), "fixture compacted context") {
		t.Fatalf("lost complete context: %s", wire)
	}
	if len(body.Messages) != 1 || string(body.Messages[0]) != string(native) {
		t.Fatal("preparation mutated the compaction input")
	}
}
