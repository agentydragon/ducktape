package session

import (
	"strings"
	"testing"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"google.golang.org/protobuf/types/known/structpb"
)

func TestCodeModeActionDescriptionMarksUnenforcedSchemas(t *testing.T) {
	action := &pb.Action{Name: "search_nodes", Description: "Find nodes", InputSchema: &structpb.Struct{}, InputSchemaValidation: &pb.InputSchemaValidation{
		Status: pb.InputSchemaValidationStatus_INPUT_SCHEMA_VALIDATION_STATUS_NOT_ENFORCED,
	}}
	got, err := hostActionDefinition("tana", action)
	if err != nil || !strings.Contains(got.Description, "does not validate arguments") || !strings.Contains(got.Description, "Action input JSON Schema") {
		t.Errorf("definition = %#v, error %v", got, err)
	}
	action.InputSchemaValidation = nil
	got, err = hostActionDefinition("tana", action)
	if err != nil || strings.Contains(got.Description, "does not validate arguments") {
		t.Errorf("unknown status described as not enforced: %#v, %v", got, err)
	}
}
