package producer

import (
	"testing"

	"github.com/agentydragon/ducktape/devinfra/x/schema_codegen/protobuf/samplepb"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

func TestGeneratedGoProtoJSONRoundTrip(t *testing.T) {
	encoded, err := ProtoJSON("go")
	if err != nil {
		t.Fatal(err)
	}
	decoded := &samplepb.Sample{}
	if err := protojson.Unmarshal(encoded, decoded); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(Sample("go"), decoded) {
		t.Fatalf("decoded message differs from producer: got %v", decoded)
	}
	if decoded.Sequence != 9_007_199_254_740_993 {
		t.Fatalf("int64 was not preserved: got %d", decoded.Sequence)
	}
	if decoded.Note == nil || *decoded.Note != "" {
		t.Fatalf("optional empty string presence was lost: got %v", decoded.Note)
	}
	if decoded.GetPayload() == nil || decoded.GetTextPayload() != "protobuf across languages" {
		t.Fatalf("oneof payload was not preserved: got %v", decoded.GetPayload())
	}
}
