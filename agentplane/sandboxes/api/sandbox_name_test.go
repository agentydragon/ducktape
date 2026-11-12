package api

import (
	"strings"
	"testing"
)

func TestIsValidSandboxName(t *testing.T) {
	for _, test := range []struct {
		name  string
		value string
		valid bool
	}{
		{name: "single letter", value: "a", valid: true},
		{name: "leading digit", value: "0", valid: true},
		{name: "internal hyphen", value: "a-0", valid: true},
		{name: "repeated internal hyphens", value: "a--b", valid: true},
		{name: "maximum length", value: strings.Repeat("a", MaxSandboxNameLength), valid: true},
		{name: "empty", value: "", valid: false},
		{name: "leading hyphen", value: "-a", valid: false},
		{name: "trailing hyphen", value: "a-", valid: false},
		{name: "uppercase", value: "A", valid: false},
		{name: "dot", value: "a.b", valid: false},
		{name: "underscore", value: "a_b", valid: false},
		{name: "unicode", value: "ašb", valid: false},
		{name: "too long", value: strings.Repeat("a", MaxSandboxNameLength+1), valid: false},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := IsValidSandboxName(test.value); got != test.valid {
				t.Errorf("IsValidSandboxName(%q) = %t, want %t", test.value, got, test.valid)
			}
		})
	}
}
