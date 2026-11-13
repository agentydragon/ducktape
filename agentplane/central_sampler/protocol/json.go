package protocol

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"slices"
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

func MustJSON(value any) []byte { data, _ := json.Marshal(value); return data }
func CanonicalJSON(value any) string {
	data, err := json.Marshal(value)
	if err != nil {
		return "{}"
	}
	return string(data)
}

func IsOneOf(value string, options ...string) bool {
	return slices.Contains(options, value)
}

func DecodeObject(data []byte, value any) error {
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return errors.New("protocol payload must be a JSON object")
	}
	decoder := json.NewDecoder(bytes.NewReader(trimmed))
	decoder.UseNumber()
	if err := decoder.Decode(value); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return errors.New("protocol payload has trailing JSON")
	}
	return nil
}

func Object(value any) (map[string]any, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var object map[string]any
	if err := DecodeObject(data, &object); err != nil {
		return nil, err
	}
	return object, nil
}
