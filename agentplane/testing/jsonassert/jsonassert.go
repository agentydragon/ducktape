package jsonassert

import (
	"bytes"
	"encoding/json"
	"reflect"
	"testing"
)

// Equal compares decoded JSON values without rounding large numbers.
func Equal(t testing.TB, got, want []byte) bool {
	t.Helper()
	parse := func(data []byte) any {
		decoder := json.NewDecoder(bytes.NewReader(data))
		decoder.UseNumber()
		var value any
		if err := decoder.Decode(&value); err != nil {
			t.Fatalf("decode JSON %q: %v", data, err)
		}
		return value
	}
	return reflect.DeepEqual(parse(got), parse(want))
}

// SameValue fails the test unless got and want decode to the same JSON value. Numbers decode as float64.
func SameValue(t testing.TB, got, want json.RawMessage) {
	t.Helper()
	var gotValue, wantValue any
	if err := json.Unmarshal(got, &gotValue); err != nil {
		t.Fatalf("%s: %v", got, err)
	}
	if err := json.Unmarshal(want, &wantValue); err != nil {
		t.Fatalf("%s: %v", want, err)
	}
	if !reflect.DeepEqual(gotValue, wantValue) {
		t.Errorf("got %s, want %s", got, want)
	}
}
