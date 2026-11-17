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
