package harnesshost

import (
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestRequireSessionVersion(t *testing.T) {
	for _, test := range []struct {
		name   string
		header metadata.MD
		want   codes.Code
	}{
		{"legacy", nil, codes.FailedPrecondition},
		{"unsupported", metadata.Pairs(SessionVersionMetadataKey, "2"), codes.FailedPrecondition},
		{"ambiguous", metadata.Pairs(SessionVersionMetadataKey, SessionVersion, SessionVersionMetadataKey, SessionVersion), codes.FailedPrecondition},
		{"current", metadata.Pairs(SessionVersionMetadataKey, SessionVersion), codes.OK},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := RequireSessionVersion(test.header); status.Code(err) != test.want {
				t.Fatalf("RequireSessionVersion(%v) = %v, want %v", test.header, err, test.want)
			}
		})
	}
}
