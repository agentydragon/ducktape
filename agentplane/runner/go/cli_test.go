package runner

import (
	"reflect"
	"strings"
	"testing"
)

func TestHarnessEnvironmentKeepsCredentialsExplicit(t *testing.T) {
	environment := map[string]string{
		"PATH":                 "/usr/bin",
		"ANTHROPIC_AUTH_TOKEN": "runner-secret-anthropic",
		"OPENAI_API_KEY":       "runner-secret-openai",
		"TZDIR":                "/usr/share/zoneinfo",
		"HOME":                 "/runner-home",
	}

	child, err := HarnessEnvironment(environment, []string{"PATH", "TOOL_ENDPOINT=https://tools.test"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(child, map[string]string{"PATH": "/usr/bin", "TOOL_ENDPOINT": "https://tools.test"}) {
		t.Fatalf("explicit child environment = %#v", child)
	}
	for _, secret := range []string{"ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "HOME", "TZDIR"} {
		if _, exists := child[secret]; exists {
			t.Errorf("undeclared runner variable %q leaked into harness environment", secret)
		}
	}
}

func TestHarnessEnvironmentMergeAndValidation(t *testing.T) {
	environment := map[string]string{"HOME": "/image-home", "TZDIR": "/usr/share/zoneinfo", "TOOL": "runner-value"}
	tests := []struct {
		name      string
		declared  []string
		inherited []string
		want      map[string]string
		wantError string
	}{
		{name: "absent bare name", declared: []string{"ABSENT"}, want: map[string]string{}},
		{name: "empty override", declared: []string{"TOOL="}, want: map[string]string{"TOOL": ""}},
		{name: "later override wins", declared: []string{"HOME", "HOME=/state/work"}, want: map[string]string{"HOME": "/state/work"}},
		{name: "image inherited", inherited: []string{"TZDIR"}, want: map[string]string{"TZDIR": "/usr/share/zoneinfo"}},
		{name: "missing inherited name", inherited: []string{"ABSENT"}, want: map[string]string{}},
		{name: "deployment overrides inherited", declared: []string{"TZDIR=/deployed"}, inherited: []string{"TZDIR"}, want: map[string]string{"TZDIR": "/deployed"}},
		{name: "bad declared name", declared: []string{"=value"}, wantError: "--harness-env expects NAME or NAME=value"},
		{name: "inherited values are forbidden", inherited: []string{"TZDIR=/wrong"}, wantError: "--harness-inherit-env takes a bare NAME"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := HarnessEnvironment(environment, test.declared, test.inherited)
			if test.wantError != "" {
				if err == nil || !strings.Contains(err.Error(), test.wantError) {
					t.Fatalf("HarnessEnvironment error = %v, want message containing %q", err, test.wantError)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, test.want) {
				t.Fatalf("HarnessEnvironment = %#v, want %#v", got, test.want)
			}
		})
	}
}
