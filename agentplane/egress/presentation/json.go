package presentation

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"strings"

	"github.com/go-json-experiment/json/jsontext"
)

// MaxJSONBodyBytes bounds the body a JSON credential can be presented in.
const MaxJSONBodyBytes = 64 * 1024

// ErrInvalidJSONBody means a body-target request is not an unambiguous, bounded JSON object. It
// never carries parser input: a body can hold a secret.
var ErrInvalidJSONBody = errors.New("invalid JSON body")

// IsJSONEnvelope reports whether the headers declare the only body a JSON credential can be
// presented in: one uncompressed `application/json` entity.
func IsJSONEnvelope(headers http.Header) bool {
	contentTypes := headers.Values("Content-Type")
	encodings := headers.Values("Content-Encoding")
	if len(contentTypes) != 1 {
		return false
	}
	mediaType, _, _ := strings.Cut(contentTypes[0], ";")
	return strings.EqualFold(strings.TrimSpace(mediaType), "application/json") &&
		(len(encodings) == 0 || len(encodings) == 1 && encodings[0] == "identity")
}

// JSONObject is a validated JSON object body and where its top-level string members sit in it, so
// a rewrite replaces those members and leaves every other byte as it came.
type JSONObject struct {
	body    []byte
	strings map[string]stringMember
}

type stringMember struct {
	value string
	// start and end delimit the member's string literal in the body.
	start, end int
}

// ParseJSONBody parses only an uncompressed UTF-8 `application/json` object of at most
// MaxJSONBodyBytes. It rejects duplicate member names at any depth, malformed syntax, invalid UTF-8
// and trailing data, and never includes parser input in the error. A nil body is a body that was
// not read, which no credential can be presented in.
func ParseJSONBody(headers http.Header, body []byte) (*JSONObject, error) {
	if body == nil || len(body) > MaxJSONBodyBytes || !IsJSONEnvelope(headers) {
		return nil, ErrInvalidJSONBody
	}
	// The decoder rejects duplicate names and invalid UTF-8 unless told not to.
	decoder := jsontext.NewDecoder(bytes.NewReader(body))
	if token, err := decoder.ReadToken(); err != nil || token.Kind() != '{' {
		return nil, ErrInvalidJSONBody
	}
	object := &JSONObject{body: body, strings: map[string]stringMember{}}
	for decoder.PeekKind() != '}' {
		nameToken, err := decoder.ReadToken()
		if err != nil {
			return nil, ErrInvalidJSONBody
		}
		// A token is voided by the next read from the decoder.
		name := nameToken.String()
		value, err := decoder.ReadValue()
		if err != nil {
			return nil, ErrInvalidJSONBody
		}
		if value.Kind() != '"' {
			continue
		}
		member := stringMember{end: int(decoder.InputOffset())}
		member.start = member.end - len(value)
		if err := json.Unmarshal(value, &member.value); err != nil {
			return nil, ErrInvalidJSONBody
		}
		object.strings[name] = member
	}
	if _, err := decoder.ReadToken(); err != nil {
		return nil, ErrInvalidJSONBody
	}
	if _, err := decoder.ReadToken(); !errors.Is(err, io.EOF) {
		return nil, ErrInvalidJSONBody
	}
	return object, nil
}

// hasString reports whether the top-level field is a string equal to value.
func (o *JSONObject) hasString(field, value string) bool {
	if o == nil {
		return false
	}
	member, ok := o.strings[field]
	return ok && member.value == value
}

// BodyRewrite is a JSON body to forward in place of the one sent. It holds a credential.
type BodyRewrite struct{ Content []byte }

func (BodyRewrite) String() string   { return "BodyRewrite{redacted}" }
func (BodyRewrite) GoString() string { return "BodyRewrite{redacted}" }

type bodyPresentation struct {
	document *JSONObject
	fields   []string
}

// rewrite splices the credential, as a JSON string, over each presented field's literal.
func (b *bodyPresentation) rewrite(credential string) (*BodyRewrite, error) {
	literal, err := jsontext.AppendQuote(nil, credential)
	if err != nil {
		return nil, fmt.Errorf("credential is not a JSON string: %w", err)
	}
	members := make([]stringMember, len(b.fields))
	for i, field := range b.fields {
		members[i] = b.document.strings[field]
	}
	slices.SortFunc(members, func(a, b stringMember) int { return a.start - b.start })
	var out bytes.Buffer
	copied := 0
	for _, member := range slices.Compact(members) {
		out.Write(b.document.body[copied:member.start])
		out.Write(literal)
		copied = member.end
	}
	out.Write(b.document.body[copied:])
	return &BodyRewrite{Content: out.Bytes()}, nil
}
