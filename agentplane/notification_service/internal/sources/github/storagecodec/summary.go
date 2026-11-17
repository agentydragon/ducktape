package storagecodec

import (
	"encoding/json"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model"
)

// SummaryConclusion returns a recognized lifecycle conclusion from a webhook payload, without decoding unrelated
// fields delivered by GitHub. Malformed or unexpected nested values are ignored.
func SummaryConclusion(event model.EventName, fields map[string]json.RawMessage) *string {
	var field string
	switch event {
	case model.EventCheckRun:
		field = "check_run"
	case model.EventWorkflowRun:
		field = "workflow_run"
	default:
		return nil
	}

	data, ok := fields[field]
	if !ok {
		return nil
	}
	var lifecycle struct {
		Conclusion *string `json:"conclusion"`
	}
	if err := json.Unmarshal(data, &lifecycle); err != nil || lifecycle.Conclusion == nil {
		return nil
	}
	switch *lifecycle.Conclusion {
	case "action_required", "cancelled", "failure", "neutral", "skipped", "stale", "success", "timed_out", "startup_failure":
		conclusion := *lifecycle.Conclusion
		return &conclusion
	default:
		return nil
	}
}
