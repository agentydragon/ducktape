package ingress

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// The exporter pushes a Session the Dispatcher recorded as executing as an Identity as a delegating
// actor for it, so History retains that Identity verified live rather than relayed, and a Session
// retained from before the cutover with no Identity as the sampler itself. An Identity incarnation that no longer exists is refused.
func TestTheExporterActsForTheIdentityASessionExecutesAs(t *testing.T) {
	fixture := newSamplerExportFixture(t, newSamplerExportPlan(0, 0, false), newSamplerExportPlan(0, 0, false), newSamplerExportPlan(0, 0, false))
	identity := &identitypb.Identity{Name: fixture.identity.Identity.Name, Uid: string(fixture.identity.Identity.UID)}
	sampler := auth.Account{Namespace: testNamespace, Name: "test-central-sampler"}

	delegated := fixture.openSessionExecutingAs(t, identity)
	if err := fixture.source.ChangeModels(t.Context(), delegated, 2, "test-model-delegated"); err != nil {
		t.Fatal(err)
	}
	ctx, _ := samplerExportContext(t)
	if err := samplerExportResult(t, startSamplerExport(ctx, fixture, delegated)); err != nil {
		t.Fatalf("export acting for the Identity: %v", err)
	}
	cursor := assertSamplerHistoryMatchesSource(t, fixture, delegated, "test-model-delegated")
	assertSamplerExportCheckpoint(t, fixture, delegated, &cursor)
	var writerName, identityName, identityUID string
	if err := fixture.historyDB.QueryRowContext(t.Context(), "SELECT writer_name, execution_identity_name, execution_identity_uid FROM session_history WHERE id=$1", delegated).
		Scan(&writerName, &identityName, &identityUID); err != nil {
		t.Fatal(err)
	}
	if writerName != sampler.Name || identityName != identity.Name || identityUID != identity.Uid {
		t.Fatalf("History binding writer=%s identity=%s/%s, want the sampler acting for %v", writerName, identityName, identityUID, identity)
	}

	direct := fixture.openLegacySession(t)
	if err := fixture.source.ChangeModels(t.Context(), direct, 1, "test-model-direct"); err != nil {
		t.Fatal(err)
	}
	if err := samplerExportResult(t, startSamplerExport(ctx, fixture, direct)); err != nil {
		t.Fatalf("export as the sampler itself: %v", err)
	}
	assertSamplerHistoryMatchesSource(t, fixture, direct, "test-model-direct")

	fixture.ingress.mu.Lock()
	admissions := append([]auth.Admission(nil), fixture.ingress.admissions...)
	fixture.ingress.mu.Unlock()
	if len(admissions) != 2 {
		t.Fatalf("History admitted %d pushes, want one per Session", len(admissions))
	}
	acting, ok := admissions[0].(auth.DelegatedIdentityAdmission)
	if !ok || acting.Actor.Account != sampler || acting.EffectiveCaller != (auth.Identity{Name: identity.Name, UID: identity.Uid}) ||
		acting.Provenance.GetSession().GetSessionId() != delegated {
		t.Fatalf("the Identity Session's push was admitted as %#v, want %v acting for %v", admissions[0], sampler, identity)
	}
	if _, ok := admissions[1].(auth.DirectAdmission); !ok {
		t.Fatalf("the Identity-less Session's push was admitted as %#v, want the sampler itself", admissions[1])
	}

	if err := fixture.identity.Objects.Delete(t.Context(), fixture.identity.Identity); err != nil {
		t.Fatal(err)
	}
	if err := fixture.source.ChangeModels(t.Context(), delegated, 1, "test-model-after-deletion"); err != nil {
		t.Fatal(err)
	}
	err := samplerExportResult(t, startSamplerExport(ctx, fixture, delegated))
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("export for a deleted Identity incarnation = %v, want PermissionDenied", err)
	}
	assertSamplerPending(t, fixture, delegated, true)
}
