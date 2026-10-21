package policies

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/santhosh-tekuri/jsonschema/v6"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

// schemaURL names a policy's schema while it compiles. A policy schema is one document: nothing else resolves.
const schemaURL = "urn:agentplane:action-policy-schema"

// ArgumentSchema matches a listed Action whose arguments satisfy a JSON Schema, with plain JSON Schema semantics: the
// schema says which properties are required and which may be absent, so `properties` alone never implies presence.
type ArgumentSchema struct {
	schema *jsonschema.Schema
}

func (ArgumentSchema) Type() store.PolicyKind { return store.PolicyArgumentSchema }

// parseArgumentSchema compiles the schema, checking it against its draft's metaschema. A schema without `$schema` is
// read as draft 2020-12, and one that refers to any document but itself and the drafts' metaschemas is refused.
func parseArgumentSchema(spec policyapi.ActionPolicy) (ArgumentSchema, error) {
	if spec.Schema == nil {
		return ArgumentSchema{}, errors.New("an argument_schema policy needs a schema")
	}
	document, err := jsonschema.UnmarshalJSON(bytes.NewReader(spec.Schema.Raw))
	if err != nil {
		return ArgumentSchema{}, fmt.Errorf("schema is not JSON: %w", err)
	}
	compiler := jsonschema.NewCompiler()
	compiler.DefaultDraft(jsonschema.Draft2020)
	compiler.UseLoader(noDocuments{})
	if err := compiler.AddResource(schemaURL, document); err != nil {
		return ArgumentSchema{}, fmt.Errorf("schema is not a valid JSON Schema: %w", err)
	}
	compiled, err := compiler.Compile(schemaURL)
	if err != nil {
		return ArgumentSchema{}, fmt.Errorf("schema is not a valid JSON Schema: %w", err)
	}
	return ArgumentSchema{schema: compiled}, nil
}

// noDocuments loads no document a schema refers to; the library's default loader reads local files.
type noDocuments struct{}

func (noDocuments) Load(url string) (any, error) {
	return nil, fmt.Errorf("a policy schema cannot refer to %s", url)
}

func (k ArgumentSchema) evaluate(_ context.Context, action store.ActionIdentity, arguments map[string]any, _ *Visibility) Outcome {
	err := k.schema.Validate(arguments)
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
