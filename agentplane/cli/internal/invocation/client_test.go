package invocation

import (
	"net/url"
	"testing"
)

func TestGRPCTargetUsesTheURLDefaultPort(t *testing.T) {
	cases := []struct {
		origin string
		want   string
		bad    bool
	}{
		{"http://actions.example", "actions.example:80", false},
		{"https://actions.example", "actions.example:443", false},
		{"http://actions.example:8123", "actions.example:8123", false},
		{"https://[::1]", "[::1]:443", false},
		{"http://actions.example:", "", true},
		{"http://actions.example:0", "", true},
		{"http://actions.example:65536", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.origin, func(t *testing.T) {
			parsed, err := url.Parse(tc.origin)
			if err != nil {
				t.Fatal(err)
			}
			got, err := grpcTarget(parsed, "AGENTPLANE_ACTIONS_URL")
			if tc.bad {
				if err == nil {
					t.Fatalf("target = %q; want invalid origin", got)
				}
				return
			}
			if err != nil || got != tc.want {
				t.Fatalf("target = %q, error %v; want %q", got, err, tc.want)
			}
		})
	}
}
