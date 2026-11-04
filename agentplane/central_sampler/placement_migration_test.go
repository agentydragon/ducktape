package centralsampler

import (
	"bytes"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func TestPlacementMigrationPreservesRetainedCentralRetryAndUnknownFields(t *testing.T) {
	store := newTestStore(t)
	request := &samplerpb.CreateSessionRequest{SessionId: uuid.NewString(), Spec: &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"}}
	opened, err := openStoreSession(t, store, request, "fixture-account", protocol.APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	legacy := proto.Clone(request).(*samplerpb.CreateSessionRequest)
	legacy.Spec.Placement = nil
	oldExecutor := protowire.AppendString(protowire.AppendTag(nil, 2, protowire.BytesType), legacy.Executor.Target.Name)
	oldExecutor = protowire.AppendString(protowire.AppendTag(oldExecutor, 3, protowire.BytesType), legacy.Executor.Target.Uid)
	futureExecutor := protowire.AppendVarint(protowire.AppendTag(nil, 97, protowire.VarintType), 9)
	legacy.Executor.Target = nil
	legacy.Executor.ProtoReflect().SetUnknown(append(oldExecutor, futureExecutor...))
	oldHarness := protowire.AppendVarint(protowire.AppendTag(nil, 1, protowire.VarintType), uint64(sessionpb.Harness_HARNESS_CENTRAL_SAMPLER))
	futureSpec := protowire.AppendBytes(protowire.AppendTag(nil, 99, protowire.BytesType), []byte("future-spec"))
	legacy.Spec.ProtoReflect().SetUnknown(append(oldHarness, futureSpec...))
	futureRequest := protowire.AppendVarint(protowire.AppendTag(nil, 98, protowire.VarintType), 7)
	legacy.ProtoReflect().SetUnknown(futureRequest)
	legacyWire, err := proto.Marshal(legacy)
	if err != nil {
		t.Fatal(err)
	}
	var beforeEvents, beforeCommands int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_event WHERE session_id=$1`, request.SessionId).Scan(&beforeEvents); err != nil {
		t.Fatal(err)
	}
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_command WHERE session_id=$1`, request.SessionId).Scan(&beforeCommands); err != nil {
		t.Fatal(err)
	}
	migrationExec(t, store.DB(), `UPDATE sampler_session SET configuration=$2 WHERE id=$1`, request.SessionId, legacyWire)
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id=10`)
	if err := Migrate(t.Context(), store.DB()); err != nil {
		t.Fatal(err)
	}
	retained, err := store.GetSession(t.Context(), request.SessionId, "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	var decoded samplerpb.CreateSessionRequest
	if err := proto.Unmarshal(retained.Configuration, &decoded); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(decoded.GetSpec().GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment(), request.GetSpec().GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment()) ||
		!proto.Equal(decoded.GetExecutor().GetTarget(), request.GetExecutor().GetTarget()) ||
		!bytes.Equal(decoded.GetExecutor().ProtoReflect().GetUnknown(), futureExecutor) ||
		!bytes.Equal(decoded.GetSpec().ProtoReflect().GetUnknown(), futureSpec) ||
		!bytes.Equal(decoded.ProtoReflect().GetUnknown(), futureRequest) {
		t.Fatalf("migrated central configuration lost placement or unrelated fields: %v", &decoded)
	}
	platformInstructions := fixturePlatformInstructions
	retried, err := store.CreateSession(t.Context(), request, "fixture-account", protocol.APIStyleResponses, &platformInstructions)
	if err != nil || retried.ConversationID != opened.ConversationID || retried.ID != opened.ID {
		t.Fatalf("same-ID retry after migration = %+v, %v", retried, err)
	}
	var afterEvents, afterCommands int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_event WHERE session_id=$1`, request.SessionId).Scan(&afterEvents); err != nil {
		t.Fatal(err)
	}
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_command WHERE session_id=$1`, request.SessionId).Scan(&afterCommands); err != nil {
		t.Fatal(err)
	}
	if afterEvents != beforeEvents || afterCommands != beforeCommands {
		t.Fatalf("migration or retry replayed work: events %d -> %d, commands %d -> %d", beforeEvents, afterEvents, beforeCommands, afterCommands)
	}
}

func TestRetainedExecutorRejectsMissingOrConflictingIdentity(t *testing.T) {
	legacy := func(name, uid string) []byte {
		wire := protowire.AppendString(protowire.AppendTag(nil, 2, protowire.BytesType), name)
		return protowire.AppendString(protowire.AppendTag(wire, 3, protowire.BytesType), uid)
	}
	for _, test := range []struct {
		name   string
		owner  *identitypb.ServiceAccount
		target *identitypb.EnvironmentTarget
		wire   []byte
	}{
		{"missing UID", &identitypb.ServiceAccount{Namespace: "fixture"}, nil, legacy("sandbox", "")},
		{"missing namespace", nil, nil, legacy("sandbox", "uid")},
		{"conflicting identity", &identitypb.ServiceAccount{Namespace: "fixture"}, &identitypb.EnvironmentTarget{Namespace: "fixture", Name: "other", Uid: "uid"}, legacy("sandbox", "uid")},
	} {
		t.Run(test.name, func(t *testing.T) {
			destination := &identitypb.SandboxDestination{Owner: test.owner, Target: test.target}
			destination.ProtoReflect().SetUnknown(test.wire)
			if _, err := migrateRetainedExecutor(destination); err == nil {
				t.Fatal("accepted an incomplete or conflicting executor identity")
			}
		})
	}
}
