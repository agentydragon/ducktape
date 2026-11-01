package protocol

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
)

func CloneJSON(value []byte) json.RawMessage {
	if value == nil {
		return nil
	}
	return append(json.RawMessage(nil), value...)
}

func DecodeJSONValue(data []byte) (any, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		if err == nil {
			return nil, errors.New("JSON contains trailing data")
		}
		return nil, err
	}
	return value, nil
}

func MustJSON(value any) []byte      { data, _ := json.Marshal(value); return data }
func EqualJSON(left, right any) bool { return bytes.Equal(MustJSON(left), MustJSON(right)) }
func CanonicalJSON(value any) string {
	data, err := json.Marshal(value)
	if err != nil {
		return "{}"
	}
	return string(data)
}

func IsOneOf(value string, options ...string) bool {
	for _, option := range options {
		if value == option {
			return true
		}
	}
	return false
}
