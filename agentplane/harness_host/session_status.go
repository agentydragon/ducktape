// Package harnesshost defines the HarnessHost Session status wire handshake.
package harnesshost

import (
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const (
	SessionVersionMetadataKey = "x-agentplane-harness-host-session-version"
	SessionVersion            = "1"
)

// RequireSessionVersion must run before receiving any WatchSessions frames: the
// preceding Runtime used SessionSummary with incompatible fields at the same numbers.
func RequireSessionVersion(header metadata.MD) error {
	versions := header.Get(SessionVersionMetadataKey)
	if len(versions) != 1 || versions[0] != SessionVersion {
		return status.Error(codes.FailedPrecondition, "HarnessHost Session status version is unavailable or unsupported")
	}
	return nil
}
