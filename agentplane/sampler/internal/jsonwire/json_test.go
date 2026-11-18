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
