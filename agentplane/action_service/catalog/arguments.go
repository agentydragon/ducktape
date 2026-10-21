package catalog

import (
	"bytes"
	"errors"
	"fmt"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

// inputSchemaURL names an input schema while it compiles. It is one document: nothing else resolves.
const inputSchemaURL = "urn:agentplane:action-input-schema"

// ErrInvalidArguments is arguments that do not satisfy their Action's input schema. Why they do not is not kept: the
// validator's account of it can quote the arguments back.
var ErrInvalidArguments = errors.New("arguments do not match the advertised Action schema")

// annotatedFormats are the formats the library asserts in schemas of drafts before 2019-09. Python's
// jsonschema.validate, which admission calls there, asserts no format in any draft, so each is registered as an
// annotation. The library still asserts `regex`, which it does not look up.
var annotatedFormats = []string{
	"date", "date-time", "duration", "email", "hostname", "ipv4", "ipv6", "iri", "iri-reference", "json-pointer",
	"period", "relative-json-pointer", "semver", "time", "uri", "uri-reference", "uri-template", "uuid",
}

// CheckArguments checks arguments, decoded as jsonschema.UnmarshalJSON decodes JSON, against the Action's input
// schema. It returns nil when they satisfy it, ErrInvalidArguments when they do not, and another error when the
// schema is not a valid JSON Schema. A schema without `$schema` is read as draft 2020-12, as Python's
// jsonschema.validate reads it, and one that refers to any document but itself and the drafts' metaschemas is not
// valid.
func (a ActionDefinition) CheckArguments(arguments any) error {
	document, err := jsonschema.UnmarshalJSON(bytes.NewReader(a.InputSchema))
	if err != nil {
		return fmt.Errorf("input schema is not JSON: %w", err)
	}
	compiler := jsonschema.NewCompiler()
	compiler.DefaultDraft(jsonschema.Draft2020)
	compiler.UseLoader(noDocuments{})
	for _, name := range annotatedFormats {
		compiler.RegisterFormat(&jsonschema.Format{Name: name, Validate: func(any) error { return nil }})
	}
	if err := compiler.AddResource(inputSchemaURL, document); err != nil {
		return fmt.Errorf("input schema: %w", err)
	}
	schema, err := compiler.Compile(inputSchemaURL)
	if err != nil {
		return fmt.Errorf("input schema: %w", err)
	}
	if schema.Validate(arguments) != nil {
		return ErrInvalidArguments
	}
	return nil
}

// noDocuments loads no document a schema refers to; the library's default loader reads local files.
type noDocuments struct{}

func (noDocuments) Load(url string) (any, error) {
	return nil, fmt.Errorf("an input schema cannot refer to %s", url)
}
