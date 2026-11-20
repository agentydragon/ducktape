package catalog

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sync"
	"time"

	"github.com/dlclark/regexp2/v2"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
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

// ErrPatternEvaluation means a fallback regular expression exceeded its budget or failed. The result is not a
// mismatch: a mismatch may be negated by `not`, select a branch, or ignore a patternProperties entry.
var ErrPatternEvaluation = errors.New("argument schema regular expression could not be evaluated")

// patternBudget sets a shared approximate deadline for backtracking matches during one validation.
// RE2 remains the default for every pattern it accepts. Backtracking is needed for standard lookahead and backreferences.
const patternBudget = 250 * time.Millisecond

type patternState struct {
	mu       sync.Mutex
	deadline time.Time
	failed   bool
}

// CompiledSchema keeps regexp evaluation errors associated with the right validation. jsonschema's Regexp interface
// exposes only a bool, so a per-schema mutex serializes its validations while the adapter records any match error.
// Distinct compiled schemas do not share the mutex or state.
type CompiledSchema struct {
	schema *jsonschema.Schema
	state  *patternState
}

// Validate rejects the whole validation when a regexp evaluation fails, even if `not`, `anyOf`, `if`, or
// patternProperties would otherwise turn that match failure into acceptance.
func (s *CompiledSchema) Validate(value any) error {
	s.state.mu.Lock()
	defer s.state.mu.Unlock()
	s.state.failed = false
	s.state.deadline = time.Now().Add(patternBudget)
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
	remaining := time.Until(r.state.deadline)
	if remaining <= 0 {
		r.state.failed = true
		return false
	}
	r.re.MatchTimeout = remaining
	matched, err := r.re.MatchString(value)
	if err != nil {
		r.state.failed = true
		return false
	}
	return matched
}

func patternEngine(state *patternState) func(string) (jsonschema.Regexp, error) {
	return func(expr string) (jsonschema.Regexp, error) {
		if re, err := regexp.Compile(expr); err == nil {
			return re, nil
		}
		re, err := regexp2.Compile(expr, regexp2.ECMAScript)
		if err != nil {
			return nil, err
		}
		return boundedRegexp{re: re, state: state}, nil
	}
}

// CompileSchema compiles document, a JSON Schema of an Action's arguments decoded as jsonschema.UnmarshalJSON decodes
// JSON, checking it against its draft's metaschema, so that it validates as Python's jsonschema.validate does. A
// schema without `$schema` is read as draft 2020-12, and one that refers to any document but itself and the drafts'
// metaschemas is not valid.
func CompileSchema(document any) (*CompiledSchema, error) {
	state := &patternState{}
	compiler := jsonschema.NewCompiler()
	compiler.UseRegexpEngine(patternEngine(state))
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
	if state.failed {
		return nil, ErrPatternEvaluation
	}
	if err != nil {
		return nil, err
	}
	return &CompiledSchema{schema: schema, state: state}, nil
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
	if schema.Validate(arguments) != nil {
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

// DecodeArguments reads arguments into args once they satisfy schema, a schema CompileSchema compiled; it returns
// ErrInvalidArguments when they do not.
func DecodeArguments(schema *CompiledSchema, arguments json.RawMessage, args any) error {
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
