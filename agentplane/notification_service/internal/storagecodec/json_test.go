package storagecodec

import "testing"

func TestDecodeRequiresExactlyOneJSONValue(t *testing.T) {
	for _, tc := range []struct {
		name  string
		input string
		valid bool
	}{
		{name: "one value", input: `{"value": 1}`, valid: true},
		{name: "trailing whitespace", input: "{\"value\": 1}\n\t ", valid: true},
		{name: "closing array", input: `{"value": 1}]`},
		{name: "closing object", input: `{"value": 1}}`},
		{name: "second value", input: `{"value": 1} {"value": 2}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var value struct {
				Value int `json:"value"`
			}
			err := Decode([]byte(tc.input), &value)
			if (err == nil) != tc.valid {
				t.Fatalf("Decode error = %v, valid = %v", err, tc.valid)
			}
			if tc.valid && value.Value != 1 {
				t.Fatalf("decoded value = %d, want 1", value.Value)
			}
		})
	}
}
