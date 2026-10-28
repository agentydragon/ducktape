package contract

import (
	_ "embed"
	"encoding/json"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
)

//go:embed action.schema.json
var actionVocabulary []byte

// Deployment policy enumerates the shared vocabulary; each entry must have a runtime contract.
func TestOfferedActionsMatchSharedVocabulary(t *testing.T) {
	var vocabulary struct {
		Enum []string `json:"enum"`
	}
	if err := json.Unmarshal(actionVocabulary, &vocabulary); err != nil {
		t.Fatal(err)
	}
	inputs, err := Bind(catalog.PodBinding{MaxTimeoutSeconds: maxTimeoutSeconds, MaxOutputBytes: maxOutputBytes})
	if err != nil {
		t.Fatal(err)
	}
	offered := inputs.Definitions()
	if len(offered) != len(vocabulary.Enum) {
		t.Fatalf("offered %d Actions; shared vocabulary has %d", len(offered), len(vocabulary.Enum))
	}
	for _, name := range vocabulary.Enum {
		if _, ok := offered[name]; !ok {
			t.Errorf("shared Action %q has no runtime contract", name)
		}
	}
}
