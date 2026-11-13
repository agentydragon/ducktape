package catalog

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

// schemaURL names a schema while it compiles. It is one document: nothing else resolves.
const schemaURL = "urn:agentplane:action-arguments-schema"

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

// CompileSchema compiles document, a JSON Schema of an Action's arguments decoded as jsonschema.UnmarshalJSON decodes
// JSON, checking it against its draft's metaschema, so that it validates as Python's jsonschema.validate does. A
// schema without `$schema` is read as draft 2020-12, and one that refers to any document but itself and the drafts'
// metaschemas is not valid.
func CompileSchema(document any) (*jsonschema.Schema, error) {
	compiler := jsonschema.NewCompiler()
	compiler.DefaultDraft(jsonschema.Draft2020)
	compiler.UseLoader(noDocuments{})
	for _, name := range annotatedFormats {
		compiler.RegisterFormat(&jsonschema.Format{Name: name, Validate: func(any) error { return nil }})
	}
	if err := compiler.AddResource(schemaURL, document); err != nil {
		return nil, err
	}
	return compiler.Compile(schemaURL)
}

// noDocuments loads no document a schema refers to; the library's default loader reads local files.
type noDocuments struct{}

func (noDocuments) Load(url string) (any, error) {
	return nil, fmt.Errorf("an arguments schema cannot refer to %s", url)
}

// CheckArguments checks arguments, decoded as jsonschema.UnmarshalJSON decodes JSON, against the Action's input
// schema. It returns nil when they satisfy it, ErrInvalidArguments when they do not, and another error when the
// schema is not one CompileSchema compiles.
func (a ActionDefinition) CheckArguments(arguments any) error {
	document, err := jsonschema.UnmarshalJSON(bytes.NewReader(a.InputSchema))
	if err != nil {
		return fmt.Errorf("input schema is not JSON: %w", err)
	}
	schema, err := CompileSchema(document)
	if err != nil {
		return fmt.Errorf("input schema: %w", err)
	}
	if schema.Validate(arguments) != nil {
		return ErrInvalidArguments
	}
	return nil
}

// DecodeArguments reads arguments into args once they satisfy schema, a schema CompileSchema compiled; it returns
// ErrInvalidArguments when they do not.
func DecodeArguments(schema *jsonschema.Schema, arguments json.RawMessage, args any) error {
	value, err := jsonschema.UnmarshalJSON(bytes.NewReader(arguments))
	if err == nil {
		err = schema.Validate(value)
	}
	if err == nil {
		err = json.Unmarshal(arguments, args)
	}
	if err != nil {
		return ErrInvalidArguments
	}
	return nil
}
