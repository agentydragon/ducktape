package service

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/execution/executorpb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestOwnerLockFencesContenderAndSurvivesSocketReplacement(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "service")
	owner, err := lock(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer owner.Close()
	if err := os.WriteFile(filepath.Join(directory, "execution.sock"), []byte("stale socket"), 0600); err != nil {
		t.Fatal(err)
	}
	if contender, err := lock(directory); err == nil {
		_ = contender.Close()
		t.Fatal("second service acquired the live execution owner lock")
	}
	if err := owner.Close(); err != nil {
		t.Fatal(err)
	}
	successor, err := lock(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer successor.Close()
}

func TestAdmissionRejectsForeignOrStaleScopeBeforeEffect(t *testing.T) {
	config := Config{Namespace: "test-namespace", SandboxName: "test-sandbox", SandboxUID: "test-sandbox-uid", IncarnationUID: "test-incarnation"}
	scope := &executorpb.ExecutionScope{
		Destination: &sandboxespb.SandboxDestination{
			Owner:   &sandboxespb.ServiceAccount{Namespace: config.Namespace, Name: config.SandboxName},
			Sandbox: config.SandboxName, SandboxUid: config.SandboxUID,
		}, ExpectedIncarnationUid: config.IncarnationUID, SessionId: "test-session",
	}
	for _, test := range []struct {
		name   string
		change func(*executorpb.ExecutionScope)
		code   codes.Code
	}{
		{"current", func(*executorpb.ExecutionScope) {}, codes.OK},
		{"namespace", func(s *executorpb.ExecutionScope) { s.Destination.Owner.Namespace = "another-namespace" }, codes.PermissionDenied},
		{"owner", func(s *executorpb.ExecutionScope) { s.Destination.Owner.Name = "another-owner" }, codes.PermissionDenied},
		{"sandbox", func(s *executorpb.ExecutionScope) { s.Destination.Sandbox = "another-sandbox" }, codes.PermissionDenied},
		{"uid", func(s *executorpb.ExecutionScope) { s.Destination.SandboxUid = "another-uid" }, codes.PermissionDenied},
		{"incarnation", func(s *executorpb.ExecutionScope) { s.ExpectedIncarnationUid = "old-incarnation" }, codes.FailedPrecondition},
		{"session", func(s *executorpb.ExecutionScope) { s.SessionId = "" }, codes.InvalidArgument},
	} {
		t.Run(test.name, func(t *testing.T) {
			candidate := proto.Clone(scope).(*executorpb.ExecutionScope)
			test.change(candidate)
			accepted := false
			err := config.admit(t.Context(), candidate, true, func(context.Context) error { accepted = true; return nil })
			if status.Code(err) != test.code || accepted != (test.code == codes.OK) {
				t.Fatalf("admission: accepted=%v error=%v, want %s", accepted, err, test.code)
			}
		})
	}
}
