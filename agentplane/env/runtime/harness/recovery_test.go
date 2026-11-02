package harness

import (
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

func TestRecoveryComparesArgumentsWithoutLosingNumbers(t *testing.T) {
	for _, test := range []struct {
		name, observed, recovered string
		retained                  bool
	}{
		{"formatting", `{"a":1,"b":[true,null,"text"]}`, " {\n\"b\":[true,null,\"text\"], \"a\":1 } ", true},
		{"large integer", `{"n":9007199254740992}`, `{"n":9007199254740993}`, false},
		{"precise decimal", `{"n":0.10000000000000001}`, `{"n":0.10000000000000002}`, false},
		{"number spelling", `{"n":1}`, `{"n":1.0}`, false},
		{"trailing data", `{"n":1}`, `{"n":1} {}`, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			observed := ObservedItem{ItemID: "call", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, Arguments: test.observed}
			recovered := observed
			recovered.Arguments = test.recovered
			result := CompareItem(observed, &recovered)
			want := eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_REVISED
			if test.retained {
				want = eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_RETAINED
			}
			if result.Disposition != want {
				t.Fatalf("comparison = %v, want %v", result.Disposition, want)
			}
			if !test.retained && result.Replacement.ArgumentsJson != test.recovered {
				t.Fatalf("revision lost the recovered payload: %v", result)
			}
		})
	}
}
