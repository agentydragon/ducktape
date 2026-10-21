package policies

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/santhosh-tekuri/jsonschema/v6"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

// ArgumentSchema matches a listed Action whose arguments satisfy a JSON Schema, with plain JSON Schema semantics: the
// schema says which properties are required and which may be absent, so `properties` alone never implies presence.
type ArgumentSchema struct {
	// Schema is the JSON Schema as the rule writes it.
	Schema   *structpb.Struct
	compiled *jsonschema.Schema
}

func (ArgumentSchema) Type() store.PolicyKind { return store.PolicyArgumentSchema }

// parseArgumentSchema compiles the schema as catalog.CompileSchema does.
func parseArgumentSchema(spec policyapi.ActionPolicy) (ArgumentSchema, error) {
	if spec.Schema == nil {
		return ArgumentSchema{}, errors.New("an argument_schema policy needs a schema")
	}
	document, err := jsonschema.UnmarshalJSON(bytes.NewReader(spec.Schema.Raw))
	if err != nil {
		return ArgumentSchema{}, fmt.Errorf("schema is not JSON: %w", err)
	}
	compiled, err := catalog.CompileSchema(document)
	if err != nil {
		return ArgumentSchema{}, fmt.Errorf("schema is not a valid JSON Schema: %w", err)
	}
	written := &structpb.Struct{}
	if err := protojson.Unmarshal(spec.Schema.Raw, written); err != nil {
		return ArgumentSchema{}, fmt.Errorf("schema is not a JSON object: %w", err)
	}
	return ArgumentSchema{Schema: written, compiled: compiled}, nil
}

func (k ArgumentSchema) evaluate(_ context.Context, action store.ActionIdentity, arguments map[string]any, _ *Visibility) Outcome {
	err := k.compiled.Validate(arguments)
	if err == nil {
		return Matched{Explanation: fmt.Sprintf("arguments of %s/%s satisfy the schema", action.Group, action.Name)}
	}
	reason := err.Error()
	if failed := (*jsonschema.ValidationError)(nil); errors.As(err, &failed) {
		reason = strings.Join(failures(failed), "; ")
	}
	return NotMatched{Reason: "arguments do not satisfy the schema: " + reason}
}

// failures is where and why the arguments fail, one entry per keyword that failed for its own reason.
func failures(failed *jsonschema.ValidationError) []string {
	if len(failed.Causes) == 0 {
		return []string{failed.Error()}
	}
	var leaves []string
	for _, cause := range failed.Causes {
		leaves = append(leaves, failures(cause)...)
	}
	return leaves
}
