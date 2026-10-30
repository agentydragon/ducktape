package configuration

import (
	"reflect"
	"testing"
)

func TestEnvironmentKeepsInheritanceExplicit(t *testing.T) {
	environ := map[string]string{"PATH": "/usr/bin", "OPENAI_API_KEY": "runner-secret", "TZDIR": "/image/zoneinfo", "HOME": "/image/home"}
	child, err := (EnvironmentVariables{Inherit: []string{"PATH", "TZDIR"}, Values: map[string]string{"TZDIR": "/deployment/zoneinfo"}}).resolve(environ)
	if err != nil {
		t.Fatal(err)
	}
	if want := (map[string]string{"PATH": "/usr/bin", "TZDIR": "/deployment/zoneinfo"}); !reflect.DeepEqual(child, want) {
		t.Fatalf("environment = %v, want %v", child, want)
	}
	for _, name := range []string{"", "KEY=value", "BAD\x00NAME"} {
		if _, err := (EnvironmentVariables{Inherit: []string{name}}).resolve(environ); err == nil {
			t.Fatalf("accepted invalid inherited name %q", name)
		}
	}
}
