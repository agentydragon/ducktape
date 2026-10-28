package catalog

import (
	"encoding/json"
	"math"
	"testing"
)

func TestIntegerJSON(t *testing.T) {
	for _, test := range []struct {
		input string
		want  Integer
	}{
		{"60", 60},
		{"60.0", 60},
		{"6e1", 60},
		{"6E+1", 60},
		{"-6e1", -60},
		{"-0.0", 0},
		{"0e9999999999999999999999", 0},
		{"9223372036854775807", math.MaxInt64},
		{"-9223372036854775808", math.MinInt64},
		{"9.223372036854775807e18", math.MaxInt64},
	} {
		var got Integer
		if err := json.Unmarshal([]byte(test.input), &got); err != nil || got != test.want {
			t.Errorf("Unmarshal(%s) = %d, %v; want %d", test.input, got, err, test.want)
		}
	}
	for _, input := range []string{
		"60.5", "6e-1", "true", "null", `"60"`, "[]", "{}", "",
		"9223372036854775808", "-9223372036854775809", "9.223372036854775808e18",
		"1e9999999999999999999999", "1e-9999999999999999999999", "01", "60 61",
	} {
		var got Integer
		if err := got.UnmarshalJSON([]byte(input)); err == nil {
			t.Errorf("UnmarshalJSON(%s) = %d, want error", input, got)
		}
	}
}
