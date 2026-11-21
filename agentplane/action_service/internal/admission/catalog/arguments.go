package catalog

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/dlclark/regexp2/v2"
	"github.com/santhosh-tekuri/jsonschema/v6"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
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

// ErrPatternEvaluation means a regular expression exceeded its budget or failed. The result is not a
// mismatch: a mismatch may be negated by `not`, select a branch, or ignore a patternProperties entry.
var ErrPatternEvaluation = errors.New("argument schema regular expression could not be evaluated")

// patternBudget covers waiting for the schema and evaluating its regex matches.
const patternBudget = 250 * time.Millisecond

type patternState struct {
	deadline time.Time
	failed   bool
}

// CompiledSchema keeps regexp evaluation errors associated with the right validation. jsonschema's Regexp interface
// exposes only a bool, so schemas with backtracking patterns serialize their validations while the adapter records any
// match error. Schemas without patterns validate concurrently.
type CompiledSchema struct {
	schema *jsonschema.Schema
	state  *patternState
	gate   chan struct{}
}

// Validate rejects the whole validation when a regexp evaluation fails. Waiting for the schema and evaluating
// its matches share the same deadline.
func (s *CompiledSchema) Validate(value any) error {
	if s.gate == nil {
		return s.schema.Validate(value)
	}
	deadline := time.Now().Add(patternBudget)
	timer := time.NewTimer(patternBudget)
	defer timer.Stop()
	select {
	case s.gate <- struct{}{}:
		defer func() { <-s.gate }()
	case <-timer.C:
		return ErrPatternEvaluation
	}
	if time.Until(deadline) <= 0 {
		return ErrPatternEvaluation
	}
	s.state.failed = false
	s.state.deadline = deadline
	err := s.schema.Validate(value)
	if s.state.failed {
		return ErrPatternEvaluation
	}
	return err
}

type boundedRegexp struct {
	re    *regexp2.Regexp
	state *patternState
}

func (r boundedRegexp) String() string { return r.re.String() }
func (r boundedRegexp) MatchString(value string) bool {
	if r.state.failed {
		return false
	}
	remaining := time.Until(r.state.deadline)
	if remaining <= 0 {
		r.state.failed = true
		return false
	}
	r.re.MatchTimeout = remaining
	matched, err := r.re.MatchString(value)
	if err != nil || time.Until(r.state.deadline) <= 0 {
		r.state.failed = true
		return false
	}
	return matched
}

// CompileSchema compiles document, a JSON Schema of an Action's arguments decoded as jsonschema.UnmarshalJSON decodes
// JSON. Patterns use Unicode-aware ECMAScript mode. A schema without `$schema` uses draft 2020-12. Only this
// document and the drafts' built-in metaschemas can be resolved.
func CompileSchema(document any) (*CompiledSchema, error) {
	state := &patternState{}
	// The engine also compiles `format: regex` values during validation. Those matchers are never used.
	compiling, hasPattern := true, false
	compiler := jsonschema.NewCompiler()
	compiler.UseRegexpEngine(func(expr string) (jsonschema.Regexp, error) {
		re, err := regexp2.Compile(expr, regexp2.ECMAScript|regexp2.Unicode)
		if err != nil {
			return nil, err
		}
		if compiling {
			hasPattern = true
		}
		return boundedRegexp{re: re, state: state}, nil
	})
	compiler.DefaultDraft(jsonschema.Draft2020)
	compiler.UseLoader(noDocuments{})
	for _, name := range annotatedFormats {
		compiler.RegisterFormat(&jsonschema.Format{Name: name, Validate: func(any) error { return nil }})
	}
	if err := compiler.AddResource(schemaURL, document); err != nil {
		return nil, err
	}
	state.deadline = time.Now().Add(patternBudget)
	schema, err := compiler.Compile(schemaURL)
	compiling = false
	if state.failed {
		return nil, ErrPatternEvaluation
	}
	if err != nil {
		return nil, err
	}
	var gate chan struct{}
	if hasPattern {
		gate = make(chan struct{}, 1)
	}
	return &CompiledSchema{schema: schema, state: state, gate: gate}, nil
}

// noDocuments loads no document a schema refers to; the library's default loader reads local files.
type noDocuments struct{}

func (noDocuments) Load(url string) (any, error) {
	return nil, fmt.Errorf("an arguments schema cannot refer to %s", url)
}

// CheckArguments checks arguments, decoded as jsonschema.UnmarshalJSON decodes JSON, against the Action's input
// schema. It returns nil when they satisfy it, ErrInvalidArguments when they do not, ErrPatternEvaluation when a
// pattern cannot be evaluated, and another error when the schema is not one CompileSchema compiles.
func (a ActionDefinition) CheckArguments(arguments any) error {
	if a.InputSchemaValidation.GetStatus() == action_servicepb.InputSchemaValidationStatus_INPUT_SCHEMA_VALIDATION_STATUS_NOT_ENFORCED {
		return nil
	}
	document, err := jsonschema.UnmarshalJSON(bytes.NewReader(a.InputSchema))
	if err != nil {
		return fmt.Errorf("input schema is not JSON: %w", err)
	}
	schema, err := CompileSchema(document)
	if err != nil {
		return fmt.Errorf("input schema: %w", err)
	}
	if err := schema.Validate(arguments); err != nil {
		if errors.Is(err, ErrPatternEvaluation) {
			return err
		}
		return ErrInvalidArguments
	}
	return nil
}

// InspectSchema reports enforcement only after the schema compiles; failure is unknown for configured Actions.
func InspectSchema(raw json.RawMessage) *action_servicepb.InputSchemaValidation {
	document, err := jsonschema.UnmarshalJSON(bytes.NewReader(raw))
	if err != nil {
		return nil
	}
	if _, err = CompileSchema(document); err != nil {
		return nil
	}
	return &action_servicepb.InputSchemaValidation{Status: action_servicepb.InputSchemaValidationStatus_INPUT_SCHEMA_VALIDATION_STATUS_ENFORCED}
}

// DecodeArguments reads arguments into args once they satisfy schema. It returns ErrInvalidArguments for malformed
// or mismatched arguments and ErrPatternEvaluation when a pattern cannot be evaluated.
func DecodeArguments(schema *CompiledSchema, arguments json.RawMessage, args any) error {
	value, err := jsonschema.UnmarshalJSON(bytes.NewReader(arguments))
	if err == nil {
		err = schema.Validate(value)
	}
	if err == nil {
		err = json.Unmarshal(arguments, args)
	}
	if errors.Is(err, ErrPatternEvaluation) {
		return err
	}
	if err != nil {
		return ErrInvalidArguments
	}
	return nil
}
