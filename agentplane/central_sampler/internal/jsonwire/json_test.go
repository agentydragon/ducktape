package jsonwire

import (
	"encoding/json"
	"testing"
)

func TestDecodeJSONValuePreservesNumbersAndRejectsTrailingInput(t *testing.T) {
	value, err := DecodeJSONValue([]byte(`{"count":9007199254740993}`))
	if err != nil {
		t.Fatal(err)
	}
	object, ok := value.(map[string]any)
	if !ok {
		t.Fatalf("decoded value has type %T, want object", value)
	}
	number, ok := object["count"].(json.Number)
	if !ok || number.String() != "9007199254740993" {
		t.Fatalf("decoded count is %T %v, want exact JSON number", object["count"], object["count"])
	}
	if _, err := DecodeJSONValue([]byte(`{} {}`)); err == nil || err.Error() != "JSON contains trailing data" {
		t.Fatalf("trailing JSON error = %v", err)
	}
}

func TestDecodeObjectRequiresOneObjectAndPreservesNumbers(t *testing.T) {
	var value map[string]any
	if err := DecodeObject([]byte(` {"count":9007199254740993} `), &value); err != nil {
		t.Fatal(err)
	}
	number, ok := value["count"].(json.Number)
	if !ok || number.String() != "9007199254740993" {
		t.Fatalf("decoded count is %T %v, want exact JSON number", value["count"], value["count"])
	}

	for _, input := range []string{"", "[]", "null", `"value"`, "1"} {
		t.Run(input, func(t *testing.T) {
			if err := DecodeObject([]byte(input), &value); err == nil || err.Error() != "protocol payload must be a JSON object" {
				t.Fatalf("DecodeObject(%q) error = %v", input, err)
			}
		})
	}
	for _, input := range []string{"{}{}", "{} invalid"} {
		t.Run(input, func(t *testing.T) {
			if err := DecodeObject([]byte(input), &value); err == nil || err.Error() != "protocol payload has trailing JSON" {
				t.Fatalf("DecodeObject(%q) error = %v", input, err)
			}
		})
	}
}
