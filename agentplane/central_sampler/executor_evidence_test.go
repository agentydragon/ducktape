package centralsampler

import (
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

func TestNativeFramePhaseMapsAdapterDirectionsAndRejectsOthers(t *testing.T) {
	for direction, want := range map[string]eventpb.ExecutionProgress_Phase{
		"request":      eventpb.ExecutionProgress_PHASE_REQUEST,
		"response":     eventpb.ExecutionProgress_PHASE_RESPONSE,
		"notification": eventpb.ExecutionProgress_PHASE_NOTIFICATION,
	} {
		if got, err := nativeFramePhase(direction); err != nil || got != want {
			t.Errorf("nativeFramePhase(%q) = %v, %v; want %v", direction, got, err, want)
		}
	}
	if got, err := nativeFramePhase("test-unnamed-direction"); err == nil {
		t.Errorf("nativeFramePhase accepted an unknown direction as %v", got)
	}
}
