package gateway

import (
	"encoding/json"
	"os"
	"testing"

	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/providerpb"
	"github.com/bazelbuild/rules_go/go/runfiles"
	"google.golang.org/protobuf/encoding/protojson"
)

type protocolValidationExample struct {
	Name    string          `json:"name"`
	Valid   bool            `json:"valid"`
	Message json.RawMessage `json:"message"`
}

type protocolValidationExamples struct {
	Snapshots     []protocolValidationExample `json:"snapshots"`
	ProbeResults  []protocolValidationExample `json:"probe_results"`
	ImportResults []protocolValidationExample `json:"import_results"`
	VerifyResults []protocolValidationExample `json:"verify_results"`
}

func TestSharedProtocolValidationExamples(t *testing.T) {
	path, err := runfiles.Rlocation("ducktape/platform_sessions/testdata/protocol_validation_examples.json")
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var examples protocolValidationExamples
	if err := json.Unmarshal(data, &examples); err != nil {
		t.Fatal(err)
	}

	for _, example := range examples.Snapshots {
		t.Run("snapshot/"+example.Name, func(t *testing.T) {
			var value providerpb.SessionSnapshot
			if err := protojson.Unmarshal(example.Message, &value); err != nil {
				t.Fatal(err)
			}
			if got := snapshotValid(&value); got != example.Valid {
				t.Fatalf("snapshotValid() = %t, want %t", got, example.Valid)
			}
		})
	}
	for _, example := range examples.ProbeResults {
		t.Run("probe/"+example.Name, func(t *testing.T) {
			var value providerpb.ProbeResult
			if err := protojson.Unmarshal(example.Message, &value); err != nil {
				t.Fatal(err)
			}
			if got := probeValid(&value); got != example.Valid {
				t.Fatalf("probeValid() = %t, want %t", got, example.Valid)
			}
		})
	}
	for _, example := range examples.ImportResults {
		t.Run("import/"+example.Name, func(t *testing.T) {
			var value providerpb.ImportSessionResponse
			if err := protojson.Unmarshal(example.Message, &value); err != nil {
				t.Fatal(err)
			}
			if got := importValid(&value); got != example.Valid {
				t.Fatalf("importValid() = %t, want %t", got, example.Valid)
			}
		})
	}
	for _, example := range examples.VerifyResults {
		t.Run("verify/"+example.Name, func(t *testing.T) {
			var value providerpb.VerifyCandidateResponse
			if err := protojson.Unmarshal(example.Message, &value); err != nil {
				t.Fatal(err)
			}
			if got := verifyValid(&value); got != example.Valid {
				t.Fatalf("verifyValid() = %t, want %t", got, example.Valid)
			}
		})
	}
}
