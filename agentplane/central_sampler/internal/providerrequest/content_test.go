package providerrequest

import (
	"encoding/json"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
)

func TestContentReferencesDispatchesOnlyProviderReferenceKinds(t *testing.T) {
	tests := []struct {
		name    string
		kind    content.ContentKind
		payload string
		want    []content.ContentID
	}{
		{
			name:    "Responses request profile",
			kind:    responses.ContentOpenAIRequest,
			payload: `{"instructions_ref":"instructions","tool_refs":["tool"]}`,
			want:    []content.ContentID{"instructions", "tool"},
		},
		{
			name:    "Anthropic request profile",
			kind:    anthropic.ContentAnthropicRequest,
			payload: `{"system_id":"system","tools":["tool"]}`,
			want:    []content.ContentID{"system", "tool"},
		},
		{
			name:    "provider leaf",
			kind:    responses.ContentOpenAIItem,
			payload: `{"type":"message","content_refs":["ignored"]}`,
			want:    nil,
		},
		{
			name:    "generic content",
			kind:    content.ContentText,
			payload: `"text"`,
			want:    nil,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := ContentReferences(test.kind, json.RawMessage(test.payload))
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, test.want) {
				t.Fatalf("ContentReferences() = %v, want %v", got, test.want)
			}
		})
	}
}

func TestContentReferencesReturnsMalformedProviderReferenceErrors(t *testing.T) {
	if _, err := ContentReferences(responses.ContentOpenAIRequest, json.RawMessage(`{"instructions_ref":""}`)); err == nil {
		t.Fatal("empty Responses reference was accepted")
	}
	if _, err := ContentReferences(anthropic.ContentAnthropicRequest, json.RawMessage(`{"system_id":""}`)); err == nil {
		t.Fatal("empty Anthropic reference was accepted")
	}
}
