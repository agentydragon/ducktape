package catalog

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"time"

	"github.com/dlclark/regexp2/v2"
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

// patternTimeout bounds one `pattern` match in the fallback engine, which backtracks.
const patternTimeout = time.Second

// regexpEngine compiles a schema's `pattern` (and `patternProperties` key, and `regex` format). JSON Schema patterns
// are ECMA 262, and Python's re, which admission used before, accepts lookaheads too, but the library's default,
// Go's regexp (RE2), rejects `(?!`, `(?=` and backreferences, and one such pattern in one tool's schema withdrew
// the whole MCP group (Tana's `search_nodes` email filter, 2026-11-01). So a pattern is compiled by RE2 first,
// which is linear-time and unchanged for every pattern that worked before, and only a pattern RE2 rejects falls to
// an ECMAScript-mode backtracking engine. A pattern neither accepts is invalid, as before.
func regexpEngine(expr string) (jsonschema.Regexp, error) {
	re, err := regexp.Compile(expr)
	if err == nil {
		return re, nil
	}
	fallback, ecmaErr := regexp2.Compile(expr, regexp2.ECMAScript)
	if ecmaErr != nil {
		return nil, err // RE2's error: it is the library's default and the one a schema author knows
	}
	fallback.MatchTimeout = patternTimeout
	return ecmaRegexp{fallback}, nil
}

// ecmaRegexp adapts regexp2, whose match can fail, to the library's Regexp. A match that times out or exceeds the
// backtracking limit counts as no match, so arguments that cannot be checked are refused rather than admitted.
type ecmaRegexp struct{ re *regexp2.Regexp }

func (e ecmaRegexp) String() string { return e.re.String() }

func (e ecmaRegexp) MatchString(s string) bool {
	matched, err := e.re.MatchString(s)
	return err == nil && matched
}

// CompileSchema compiles document, a JSON Schema of an Action's arguments decoded as jsonschema.UnmarshalJSON decodes
// JSON, checking it against its draft's metaschema, so that it validates as Python's jsonschema.validate does. A
// schema without `$schema` is read as draft 2020-12, and one that refers to any document but itself and the drafts'
// metaschemas is not valid.
func CompileSchema(document any) (*jsonschema.Schema, error) {
	compiler := jsonschema.NewCompiler()
	compiler.DefaultDraft(jsonschema.Draft2020)
	compiler.UseLoader(noDocuments{})
	compiler.UseRegexpEngine(regexpEngine)
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
