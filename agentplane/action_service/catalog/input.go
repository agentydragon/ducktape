package catalog

import (
	"bytes"
	"encoding/json"
	"fmt"
	"reflect"

	"github.com/invopop/jsonschema"
	// Both schema libraries use the package name jsonschema.
	validator "github.com/santhosh-tekuri/jsonschema/v6"
)

// InputOptions describes one built-in Action input. Configure receives a fresh reflected schema,
// so binding-specific constraints can be applied without changing another binding's contract.
// Title and Annotations are shared read-only metadata.
type InputOptions struct {
	Name        string
	Description string
	Title       *string
	Annotations *ToolAnnotations
	Configure   func(*jsonschema.Schema) error
}

// Input binds an Action's advertised schema and argument decoder to the same Go wire type.
// Construct it with NewInput; its zero value is not an Action.
type Input[T any] struct {
	name       string
	definition ActionDefinition
	validator  *validator.Schema
}

// NewInput reflects T as a closed JSON object, applies binding constraints, and compiles the
// resulting self-contained schema with the same validator used for submitted Action arguments.
// A field with `json:",omitempty"` is optional; `jsonschema:"nullable"` independently permits
// explicit null. A pointer alone does neither.
func NewInput[T any](options InputOptions) (Input[T], error) {
	var empty Input[T]
	if !IsKey(options.Name) {
		return empty, fmt.Errorf("Action key %q is not a lowercase identifier of at most %d characters", options.Name, maxKeyLength)
	}
	if err := checkLength("description", options.Description, 1, 20_000); err != nil {
		return empty, err
	}
	typeOf := reflect.TypeOf((*T)(nil)).Elem()
	for typeOf.Kind() == reflect.Pointer {
		typeOf = typeOf.Elem()
	}
	if typeOf.Kind() != reflect.Struct {
		return empty, fmt.Errorf("Action %s input must be a struct", options.Name)
	}
	reflector := &jsonschema.Reflector{Anonymous: true, ExpandedStruct: true}
	schema := reflector.ReflectFromType(typeOf)
	if options.Configure != nil {
		if err := options.Configure(schema); err != nil {
			return empty, fmt.Errorf("Action %s input schema: %w", options.Name, err)
		}
	}
	document, err := json.Marshal(schema)
	if err != nil {
		return empty, fmt.Errorf("Action %s input schema: %w", options.Name, err)
	}
	parsed, err := validator.UnmarshalJSON(bytes.NewReader(document))
	if err != nil {
		return empty, fmt.Errorf("Action %s input schema: %w", options.Name, err)
	}
	compiled, err := CompileSchema(parsed)
	if err != nil {
		return empty, fmt.Errorf("Action %s input schema: %w", options.Name, err)
	}
	return Input[T]{
		name: options.Name,
		definition: ActionDefinition{
			Description: options.Description,
			InputSchema: document,
			Title:       options.Title,
			Annotations: options.Annotations,
		},
		validator: compiled,
	}, nil
}

func (input Input[T]) Name() string { return input.name }

// Definition returns this input's read-only Action definition.
func (input Input[T]) Definition() ActionDefinition { return input.definition }

// Decode validates and decodes arguments into T. All malformed or rejected arguments have the
// same ErrInvalidArguments result as the existing Action decoder.
func (input Input[T]) Decode(arguments json.RawMessage) (T, error) {
	var value T
	if err := DecodeArguments(input.validator, arguments, &value); err != nil {
		return value, err
	}
	return value, nil
}
