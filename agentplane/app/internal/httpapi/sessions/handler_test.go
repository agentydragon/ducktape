package sessions

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	appbridge "github.com/agentydragon/ducktape/agentplane/app/internal/bridge"
	"github.com/agentydragon/ducktape/agentplane/app/internal/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/app/internal/settings"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

func TestSessionDefaultsMergeExplicitFieldsAndHonorSetupPresence(t *testing.T) {
	sandboxPresets := map[string]settings.SandboxPreset{"work": {ThreadPreset: "default"}}
	threadPresets := map[string]settings.ThreadPreset{
		"default": {
			Harness: "HARNESS_CODEX", Model: "model-a", CWD: "/state/workspaces/{session_id}",
			ReasoningEffort: sessionTestStringPtr("high"), Instructions: "from preset", SetupScript: "setup",
		},
	}
	sandbox := &environmentpb.EnvironmentDescriptor{Target: &identitypb.EnvironmentTarget{Name: "sandbox", Uid: "sandbox-uid"}, Configuration: &environmentpb.EnvironmentConfiguration{Preset: proto.String("work")}}
	defaults, setup, err := sessionDefaults(sandboxPresets, threadPresets, sandbox, false, nil)
	if err != nil {
		t.Fatal(err)
	}
	if defaults.GetPlacement().GetEnvironmentHosted().GetHarness() != sessionpb.Harness_HARNESS_CODEX || defaults.GetModel() != "model-a" || defaults.GetReasoningEffort() != "high" || setup == nil || *setup != "setup" {
		t.Fatalf("unexpected preset defaults: spec=%v setup=%v", defaults, setup)
	}
	merged, err := appbridge.ParseSessionSpec(defaults, map[string]any{"model": "", "instructions": "request"})
	if err != nil {
		t.Fatal(err)
	}
	merged.Cwd = replaceSessionID(merged.GetCwd(), "b351b316-fdc4-4f50-a642-758992ae3323")
	if merged.GetModel() != "" || merged.GetInstructions() != "request" || merged.GetCwd() != "/state/workspaces/b351b316-fdc4-4f50-a642-758992ae3323" {
		t.Fatalf("request did not overlay defaults: %v", merged)
	}
	if _, setup, err = sessionDefaults(sandboxPresets, threadPresets, sandbox, true, nil); err != nil || setup != nil {
		t.Fatalf("explicit null setup_script should clear preset value; setup=%v err=%v", setup, err)
	}
	requested := "alternate"
	if _, setup, err = sessionDefaults(sandboxPresets, threadPresets, sandbox, true, &requested); err != nil || setup == nil || *setup != requested {
		t.Fatalf("explicit setup_script should replace preset value; setup=%v err=%v", setup, err)
	}
	missingPreset := &environmentpb.EnvironmentDescriptor{Configuration: &environmentpb.EnvironmentConfiguration{Preset: proto.String("missing")}}
	if _, _, err = sessionDefaults(sandboxPresets, threadPresets, missingPreset, false, nil); err == nil {
		t.Fatal("unknown Sandbox preset was accepted")
	}
}

func TestSessionRPCErrorClassifiesWithoutHTTP(t *testing.T) {
	resource := schema.GroupResource{Group: "agentplane.example", Resource: "sandboxes"}
	for _, test := range []struct {
		name       string
		err        error
		wantCode   codes.Code
		wantDetail string
	}{
		{"sandbox missing", status.Error(codes.NotFound, "demo"), codes.NotFound, "demo"},
		{"kubernetes missing", apierrors.NewNotFound(resource, "demo"), codes.NotFound, "demo"},
		{"kubernetes conflict", apierrors.NewConflict(resource, "demo", errors.New("conflict")), codes.FailedPrecondition, "demo"},
		{"quiescing", &sandboxes.SandboxQuiescingError{Name: "demo"}, codes.Unavailable, "quiescing"},
		{"canceled", context.Canceled, codes.Canceled, "canceled"},
		{"deadline", context.DeadlineExceeded, codes.DeadlineExceeded, "Upstream service unavailable"},
		{"resource exhausted", status.Error(codes.ResourceExhausted, "quota"), codes.ResourceExhausted, "quota"},
		{"grpc aborted", fmt.Errorf("upstream: %w", status.Error(codes.Aborted, "aborted")), codes.Aborted, "aborted"},
		{"grpc already exists", status.Error(codes.AlreadyExists, "existing"), codes.AlreadyExists, "existing"},
		{"grpc internal", status.Error(codes.Internal, "private-upstream-diagnostic"), codes.Internal, "Upstream service unavailable"},
		{"unknown error", errors.New("private-upstream-diagnostic"), codes.Unavailable, "Upstream service unavailable"},
	} {
		t.Run(test.name, func(t *testing.T) {
			got := status.Convert(sessionRPCError(test.err))
			if got.Code() != test.wantCode || !strings.Contains(got.Message(), test.wantDetail) || strings.Contains(got.Message(), "private-upstream-diagnostic") {
				t.Fatalf("got %v, want %v containing %q without private details", got, test.wantCode, test.wantDetail)
			}
		})
	}
}

func sessionTestStringPtr(value string) *string { return &value }
