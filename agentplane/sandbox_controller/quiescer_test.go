package sandboxcontroller

import (
	"testing"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
)

func TestQuiesceResultTreatsOnlyQuiescedAsFinal(t *testing.T) {
	stageResponse := func(stage runnerpb.QuiesceStage) *runnerpb.QuiesceResponse {
		return &runnerpb.QuiesceResponse{Stage: stage}
	}

	tests := []struct {
		name      string
		response  *runnerpb.QuiesceResponse
		quiescing bool
	}{
		{
			name:      "draining",
			response:  stageResponse(runnerpb.QuiesceStage_QUIESCE_STAGE_DRAINING),
			quiescing: true,
		},
		{
			name:      "flushing",
			response:  stageResponse(runnerpb.QuiesceStage_QUIESCE_STAGE_FLUSHING),
			quiescing: true,
		},
		{
			name:     "quiesced",
			response: stageResponse(runnerpb.QuiesceStage_QUIESCE_STAGE_QUIESCED),
		},
		{
			name:      "unknown stage",
			response:  stageResponse(runnerpb.QuiesceStage(99)),
			quiescing: true,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := quiesceResult(test.response).Quiescing; got != test.quiescing {
				t.Errorf("quiescing = %t, want %t", got, test.quiescing)
			}
		})
	}
}
