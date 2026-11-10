package ingress

import (
	"testing"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// The exporter pushes a Session the Dispatcher recorded as executing as an Identity as a delegating
// actor for it, so History retains that Identity verified live rather than relayed, and a Session
// with no Identity as the sampler itself. An Identity incarnation that no longer exists is refused.
func TestTheExporterActsForTheIdentityASessionExecutesAs(t *testing.T) {
	fixture := newSamplerExportFixture(t, newSamplerExportPlan(0, 0, false), newSamplerExportPlan(0, 0, false), newSamplerExportPlan(0, 0, false))
	identity := &identitypb.Identity{Name: fixture.identity.Identity.Name, Uid: string(fixture.identity.Identity.UID)}
	sampler := auth.Account{Namespace: testNamespace, Name: "test-central-sampler"}

	delegated := fixture.openSessionExecutingAs(t, identity)
	updateSamplerModel(t, fixture.sampler, delegated, 2, "test-model-delegated")
	ctx, _ := samplerExportContext(t)
	if err := samplerExportResult(t, startSamplerExport(ctx, fixture, delegated.ID)); err != nil {
		t.Fatalf("export acting for the Identity: %v", err)
	}
	cursor := assertSamplerHistoryMatchesSource(t, fixture, delegated.ID, "test-model-delegated")
	assertSamplerExportCheckpoint(t, fixture, delegated.ID, &cursor)
	var writerName, identityName, identityUID string
	if err := fixture.historyDB.QueryRowContext(t.Context(), "SELECT writer_name, execution_identity_name, execution_identity_uid FROM session_history WHERE id=$1", delegated.ID).
		Scan(&writerName, &identityName, &identityUID); err != nil {
		t.Fatal(err)
	}
	if writerName != sampler.Name || identityName != identity.Name || identityUID != identity.Uid {
		t.Fatalf("History binding writer=%s identity=%s/%s, want the sampler acting for %v", writerName, identityName, identityUID, identity)
	}

	direct := fixture.openSession(t)
	updateSamplerModel(t, fixture.sampler, direct, 1, "test-model-direct")
	if err := samplerExportResult(t, startSamplerExport(ctx, fixture, direct.ID)); err != nil {
		t.Fatalf("export as the sampler itself: %v", err)
	}
	assertSamplerHistoryMatchesSource(t, fixture, direct.ID, "test-model-direct")

	fixture.ingress.mu.Lock()
	admissions := append([]auth.Admission(nil), fixture.ingress.admissions...)
	fixture.ingress.mu.Unlock()
	if len(admissions) != 2 {
		t.Fatalf("History admitted %d pushes, want one per Session", len(admissions))
	}
	acting, ok := admissions[0].(auth.DelegatedIdentityAdmission)
	if !ok || acting.Actor.Account != sampler || acting.EffectiveCaller != (auth.Identity{Name: identity.Name, UID: identity.Uid}) ||
		acting.Provenance.GetSession().GetSessionId() != delegated.ID {
		t.Fatalf("the Identity Session's push was admitted as %#v, want %v acting for %v", admissions[0], sampler, identity)
	}
	if _, ok := admissions[1].(auth.DirectAdmission); !ok {
		t.Fatalf("the Identity-less Session's push was admitted as %#v, want the sampler itself", admissions[1])
	}

	if err := fixture.identity.Objects.Delete(t.Context(), fixture.identity.Identity); err != nil {
		t.Fatal(err)
	}
	updateSamplerModel(t, fixture.sampler, delegated, 1, "test-model-after-deletion")
	err := samplerExportResult(t, startSamplerExport(ctx, fixture, delegated.ID))
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("export for a deleted Identity incarnation = %v, want PermissionDenied", err)
	}
	assertSamplerPending(t, fixture, delegated.ID, true)
}
