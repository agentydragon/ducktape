package presentation

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// MaxJSONBodyBytes bounds the body a JSON credential can be presented in.
const MaxJSONBodyBytes = 64 * 1024

const maxJSONDepth = 1000

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

// JSONObject is a parsed JSON object that remembers its key order.
type JSONObject struct {
	keys   []string
	fields map[string]any
}

// ParseJSONBody parses only an uncompressed UTF-8 `application/json` object of at most
// MaxJSONBodyBytes. It rejects duplicate keys at any depth, non-finite or overflowing numbers and
// trailing data, and never includes parser input in the error. A nil body is a body that was not
// read, which no credential can be presented in.
func ParseJSONBody(headers http.Header, body []byte) (*JSONObject, error) {
	if body == nil || len(body) > MaxJSONBodyBytes || !IsJSONEnvelope(headers) || !utf8.Valid(body) {
		return nil, ErrInvalidJSONBody
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	token, err := decoder.Token()
	if err != nil || token != json.Delim('{') {
		return nil, ErrInvalidJSONBody
	}
	object, err := readObject(decoder, 1)
	if err != nil {
		return nil, ErrInvalidJSONBody
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return nil, ErrInvalidJSONBody
	}
	return object, nil
}

// readObject reads the members of an object whose opening delimiter has been consumed.
func readObject(decoder *json.Decoder, depth int) (*JSONObject, error) {
	if depth > maxJSONDepth {
		return nil, ErrInvalidJSONBody
	}
	object := &JSONObject{fields: make(map[string]any)}
	for decoder.More() {
		token, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		key, ok := token.(string)
		if !ok {
			return nil, ErrInvalidJSONBody
		}
		if _, duplicate := object.fields[key]; duplicate {
			return nil, ErrInvalidJSONBody
		}
		value, err := readValue(decoder, depth)
		if err != nil {
			return nil, err
		}
		object.keys = append(object.keys, key)
		object.fields[key] = value
	}
	if _, err := decoder.Token(); err != nil {
		return nil, err
	}
	return object, nil
}

func readValue(decoder *json.Decoder, depth int) (any, error) {
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	switch token := token.(type) {
	case json.Delim:
		if token == '{' {
			return readObject(decoder, depth+1)
		}
		if depth+1 > maxJSONDepth {
			return nil, ErrInvalidJSONBody
		}
		var items []any
		for decoder.More() {
			item, err := readValue(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			items = append(items, item)
		}
		if _, err := decoder.Token(); err != nil {
			return nil, err
		}
		return items, nil
	case json.Number:
		// An integer is exact at any size; a float must be finite.
		if strings.ContainsAny(string(token), ".eE") {
			if _, err := strconv.ParseFloat(string(token), 64); err != nil {
				return nil, ErrInvalidJSONBody
			}
		}
		return token, nil
	default:
		return token, nil
	}
}

// hasString reports whether the top-level field is a string equal to value.
func (o *JSONObject) hasString(field, value string) bool {
	if o == nil {
		return false
	}
	got, ok := o.fields[field].(string)
	return ok && got == value
}

// BodyRewrite is a JSON body to forward in place of the one sent. It holds a credential.
type BodyRewrite struct{ Content []byte }

func (BodyRewrite) String() string { return "BodyRewrite{redacted}" }

type bodyPresentation struct {
	document *JSONObject
	fields   []string
}

func (b *bodyPresentation) rewrite(credential string) *BodyRewrite {
	replaced := make(map[string]struct{}, len(b.fields))
	for _, field := range b.fields {
		replaced[field] = struct{}{}
	}
	var out bytes.Buffer
	out.WriteByte('{')
	for i, key := range b.document.keys {
		if i > 0 {
			out.WriteByte(',')
		}
		writeJSONString(&out, key)
		out.WriteByte(':')
		if _, ok := replaced[key]; ok {
			writeJSONString(&out, credential)
		} else {
			writeJSONValue(&out, b.document.fields[key])
		}
	}
	out.WriteByte('}')
	return &BodyRewrite{Content: out.Bytes()}
}

// writeJSONValue writes compact, ASCII-only JSON.
func writeJSONValue(out *bytes.Buffer, value any) {
	switch value := value.(type) {
	case nil:
		out.WriteString("null")
	case bool:
		out.WriteString(strconv.FormatBool(value))
	case json.Number:
		out.WriteString(string(value))
	case string:
		writeJSONString(out, value)
	case []any:
		out.WriteByte('[')
		for i, item := range value {
			if i > 0 {
				out.WriteByte(',')
			}
			writeJSONValue(out, item)
		}
		out.WriteByte(']')
	case *JSONObject:
		out.WriteByte('{')
		for i, key := range value.keys {
			if i > 0 {
				out.WriteByte(',')
			}
			writeJSONString(out, key)
			out.WriteByte(':')
			writeJSONValue(out, value.fields[key])
		}
		out.WriteByte('}')
	}
}

func writeJSONString(out *bytes.Buffer, s string) {
	out.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"':
			out.WriteString(`\"`)
		case r == '\\':
			out.WriteString(`\\`)
		case r == '\n':
			out.WriteString(`\n`)
		case r == '\r':
			out.WriteString(`\r`)
		case r == '\t':
			out.WriteString(`\t`)
		case r == '\b':
			out.WriteString(`\b`)
		case r == '\f':
			out.WriteString(`\f`)
		case r < 0x20:
			out.WriteString(`\u00`)
			out.WriteByte("0123456789abcdef"[r>>4])
			out.WriteByte("0123456789abcdef"[r&0xf])
		case r < 0x80:
			out.WriteRune(r)
		case r >= 0x10000:
			high, low := utf16.EncodeRune(r)
			writeEscape(out, high)
			writeEscape(out, low)
		default:
			writeEscape(out, r)
		}
	}
	out.WriteByte('"')
}

func writeEscape(out *bytes.Buffer, r rune) {
	const digits = "0123456789abcdef"
	out.WriteString(`\u`)
	out.WriteByte(digits[r>>12&0xf])
	out.WriteByte(digits[r>>8&0xf])
	out.WriteByte(digits[r>>4&0xf])
	out.WriteByte(digits[r&0xf])
}
