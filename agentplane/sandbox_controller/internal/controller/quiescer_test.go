package sandboxcontroller

import (
	"testing"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func TestQuiesceResultTreatsOnlyQuiescedAsFinal(t *testing.T) {
	stageResponse := func(stage sessionpb.QuiesceStage) *sessionpb.QuiesceResponse {
		return &sessionpb.QuiesceResponse{Stage: stage}
	}

	tests := []struct {
		name      string
		response  *sessionpb.QuiesceResponse
		quiescing bool
	}{
		{
			name:      "draining",
			response:  stageResponse(sessionpb.QuiesceStage_QUIESCE_STAGE_DRAINING),
			quiescing: true,
		},
		{
			name:      "flushing",
			response:  stageResponse(sessionpb.QuiesceStage_QUIESCE_STAGE_FLUSHING),
			quiescing: true,
		},
		{
			name:     "quiesced",
			response: stageResponse(sessionpb.QuiesceStage_QUIESCE_STAGE_QUIESCED),
		},
		{
			name:      "unknown stage",
			response:  stageResponse(sessionpb.QuiesceStage(99)),
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
