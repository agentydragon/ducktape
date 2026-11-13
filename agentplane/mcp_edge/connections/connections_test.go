package connections

import (
	"strings"
	"sync"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/api/connection/connection_servicepb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// accountOf is the historical ServiceAccount attribution in a grant.
func accountOf(account *identitypb.ServiceAccount) *auth.Account {
	if account == nil {
		return nil
	}
	return &auth.Account{Namespace: account.GetNamespace(), Name: account.GetName()}
}

func wantBound(t *testing.T, what string, c *pb.Connection, want *auth.Identity) {
	t.Helper()
	var got *auth.Identity
	if identity := c.GetBoundIdentity(); identity != nil {
		got = &auth.Identity{Name: identity.GetName(), UID: identity.GetUid()}
	}
	if (got == nil) != (want == nil) || got != nil && *got != *want {
		t.Errorf("%s: bound to %v, want %v", what, got, want)
	}
}

func identityMessage(identity auth.Identity) *identitypb.Identity {
	return &identitypb.Identity{Name: identity.Name, Uid: identity.UID}
}

func TestConnectionsAreListedOldestFirstWithEveryGrantInRevisionOrder(t *testing.T) {
	s := serve(t)
	start := now().Add(-time.Hour)
	newer := s.connect(t, "test-newer", start.Add(time.Minute), &callerIdentity,
		storedGrant{caller: auth.Account{Namespace: "legacy", Name: "old"}, status: grantRevoked},
		storedGrant{identity: &callerIdentity, status: grantActive})
	older := s.connect(t, "test-older", start, nil, storedGrant{caller: auth.Account{Namespace: "legacy", Name: "old"}, status: grantRevoked})
	listed, err := s.client.ListConnections(as(t, testOperator), &pb.ListConnectionsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	got := listed.GetConnections()
	if len(got) != 2 || got[0].GetId() != older.String() || got[1].GetId() != newer.String() {
		t.Fatalf("listed %v, want %s then %s", got, older, newer)
	}
	wantBound(t, "the unbound Connection", got[0], nil)
	wantBound(t, "the bound Connection", got[1], &callerIdentity)
	grants := got[1].GetGrants()
	if len(grants) != 2 ||
		grants[0].GetRevision() != 1 || grants[0].GetStatus() != pb.GrantStatus_GRANT_STATUS_REVOKED ||
		*accountOf(grants[0].GetCaller()) != (auth.Account{Namespace: "legacy", Name: "old"}) || grants[0].GetRevokedAt() == nil ||
		grants[1].GetRevision() != 2 || grants[1].GetStatus() != pb.GrantStatus_GRANT_STATUS_ACTIVE ||
		grants[1].GetIdentity().GetName() != callerIdentity.Name || grants[1].GetIdentity().GetUid() != callerIdentity.UID ||
		grants[1].GetCaller() != nil || grants[1].GetActivatedAt() == nil || grants[1].GetRevokedAt() != nil {
		t.Errorf("grants %v", grants)
	}

	_, err = s.client.GetConnection(as(t, testOperator), &pb.GetConnectionRequest{ConnectionId: uuid.NewString()})
	wantCode(t, "get an unknown Connection", err, codes.NotFound)
	_, err = s.client.GetConnection(as(t, testOperator), &pb.GetConnectionRequest{ConnectionId: "test-not-a-uuid"})
	wantCode(t, "get by a malformed id", err, codes.InvalidArgument)
}

func TestARenameTrimsTheNameAndNeedsTheCurrentVersion(t *testing.T) {
	s := serve(t)
	c := s.active(t, "test-named", callerIdentity)
	rename := func(version int64, name string) (*pb.UpdateConnectionResponse, error) {
		return s.client.UpdateConnection(as(t, testOperator), &pb.UpdateConnectionRequest{
			ConnectionId: c.GetId(), ExpectedVersion: version, DisplayName: name,
		})
	}
	renamed, err := rename(1, "  test-renamed\t")
	if err != nil {
		t.Fatal(err)
	}
	if got := renamed.GetConnection(); got.GetDisplayName() != "test-renamed" || got.GetVersion() != 2 ||
		!got.GetUpdatedAt().AsTime().After(c.GetUpdatedAt().AsTime()) {
		t.Errorf("renamed to %v", got)
	}
	_, err = rename(1, "test-stale")
	wantCode(t, "a rename at a stale version", err, codes.Aborted)
	// The limit counts characters, not bytes.
	if _, err := rename(2, strings.Repeat("é", 200)); err != nil {
		t.Errorf("a name of 200 characters: %v", err)
	}
	for what, name := range map[string]string{
		"blank":          " \t ",
		"201 characters": strings.Repeat("é", 201),
	} {
		_, err := rename(3, name)
		wantCode(t, "a "+what+" name", err, codes.InvalidArgument)
	}
	_, err = rename(0, "test-unversioned")
	wantCode(t, "a rename at version 0", err, codes.InvalidArgument)
	_, err = s.client.UpdateConnection(as(t, testOperator), &pb.UpdateConnectionRequest{
		ConnectionId: uuid.NewString(), ExpectedVersion: 1, DisplayName: "test-unknown",
	})
	wantCode(t, "a rename of an unknown Connection", err, codes.NotFound)
}

func TestOfConcurrentRenamesAtOneVersionOnlyOneIsApplied(t *testing.T) {
	s := serve(t)
	c := s.active(t, "test-contested", callerIdentity)
	const renames = 8
	codesSeen := make([]codes.Code, renames)
	var wg sync.WaitGroup
	for i := range renames {
		wg.Go(func() {
			_, err := s.client.UpdateConnection(as(t, testOperator), &pb.UpdateConnectionRequest{
				ConnectionId: c.GetId(), ExpectedVersion: 1, DisplayName: "test-rename-" + string(rune('a'+i)),
			})
			codesSeen[i] = status.Code(err)
		})
	}
	wg.Wait()
	counts := map[codes.Code]int{}
	for _, code := range codesSeen {
		counts[code]++
	}
	if counts[codes.OK] != 1 || counts[codes.Aborted] != renames-1 {
		t.Errorf("answers %v, want one OK and the rest Aborted", counts)
	}
	if got := s.get(t, uuid.MustParse(c.GetId())); got.GetVersion() != 2 {
		t.Errorf("version %d after one rename", got.GetVersion())
	}
}

func TestARebindRetargetsAnActiveConnectionToALabeledAccount(t *testing.T) {
	s := serve(t)
	c := s.active(t, "test-rebound", callerIdentity)
	rebind := func(id string, version int64, identity auth.Identity) (*pb.RebindConnectionResponse, error) {
		return s.client.RebindConnection(as(t, testOperator), &pb.RebindConnectionRequest{
			ConnectionId: id, ExpectedVersion: version, Identity: identityMessage(identity),
		})
	}
	rebound, err := rebind(c.GetId(), 1, otherIdentity)
	if err != nil {
		t.Fatal(err)
	}
	got := rebound.GetConnection()
	wantBound(t, "the rebound Connection", got, &otherIdentity)
	// The grant keeps the Identity consent chose.
	if got.GetVersion() != 2 || got.GetGrants()[0].GetIdentity().GetName() != callerIdentity.Name {
		t.Errorf("rebound %v", got)
	}
	same, err := rebind(c.GetId(), 2, otherIdentity)
	if err != nil || same.GetConnection().GetVersion() != 2 {
		t.Errorf("a rebind to the bound account: %v, %v; want it unchanged", same, err)
	}
	_, err = rebind(c.GetId(), 1, callerIdentity)
	wantCode(t, "a rebind at a stale version", err, codes.Aborted)
	_, err = rebind(c.GetId(), 2, unavailableIdentity)
	wantCode(t, "a rebind to a non-live Identity", err, codes.FailedPrecondition)
	_, err = s.client.RebindConnection(as(t, testOperator), &pb.RebindConnectionRequest{
		ConnectionId: c.GetId(), ExpectedVersion: 2, Identity: &identitypb.Identity{Name: callerIdentity.Name},
	})
	wantCode(t, "a rebind without a UID", err, codes.InvalidArgument)

	pending := s.connect(t, "test-pending", now(), &callerIdentity, storedGrant{identity: &callerIdentity, status: grantPending})
	_, err = rebind(pending.String(), 1, otherIdentity)
	wantCode(t, "a rebind of a Connection with only a pending grant", err, codes.FailedPrecondition)
	unbound := s.connect(t, "test-unbound", now(), nil, storedGrant{identity: &callerIdentity, status: grantRevoked})
	_, err = rebind(unbound.String(), 1, otherIdentity)
	wantCode(t, "a rebind of an unbound Connection", err, codes.FailedPrecondition)

	// A view of the callers that is not current vouches for no account.
	s.identities.goStale()
	_, err = rebind(c.GetId(), 2, callerIdentity)
	wantCode(t, "a rebind while the view of Identities is stale", err, codes.FailedPrecondition)
}

func TestLegacyConnectionFailsClosedUntilExplicitIdentityRebind(t *testing.T) {
	s := serve(t)
	id := uuid.New()
	created := now().Add(-time.Hour)
	legacy := auth.Account{Namespace: "legacy", Name: "caller"}
	if _, err := s.db.ExecContext(t.Context(), `
		INSERT INTO external_connection (id, display_name, legacy_bound_caller, identity_binding_required, version, created_at, updated_at)
		VALUES ($1, 'test-legacy', $2::jsonb, true, 1, $3, $3)`, id, accountJSON(legacy), created); err != nil {
		t.Fatal(err)
	}
	grantID := uuid.New()
	activated := created.Add(time.Minute)
	if _, err := s.db.ExecContext(t.Context(), `
		INSERT INTO external_connection_grant (id, connection_id, revision, original_caller, issuer, client_id, status,
			created_at, activated_at)
		VALUES ($1, $2, 1, $3::jsonb, 'https://test-actions.example', 'legacy-client', 'active', $4, $5)`,
		grantID, id, accountJSON(legacy), created, activated); err != nil {
		t.Fatal(err)
	}
	tx, err := s.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.store.ResolveGrant(t.Context(), tx, grantID, "https://test-actions.example", "legacy-client")
	if err != refusedLegacy {
		t.Fatalf("legacy grant resolved as %v, %v; want fail-closed legacy refusal", err, status.Code(err))
	}
	_ = tx.Rollback()

	before := s.get(t, id)
	if !before.GetIdentityBindingRequired() || before.GetLegacyBoundCaller().GetNamespace() != legacy.Namespace ||
		before.GetGrants()[0].GetId() != grantID.String() || before.GetGrants()[0].GetCaller().GetName() != legacy.Name ||
		before.GetGrants()[0].GetIdentity() != nil {
		t.Fatalf("legacy audit/binding data was not preserved: %+v", before)
	}
	if _, err := s.db.ExecContext(t.Context(), `UPDATE external_connection SET bound_caller = $2::jsonb WHERE id = $1`,
		id, accountJSON(legacy)); err == nil {
		t.Fatal("an old binary restored active ServiceAccount authority after the cutover")
	}
	rebound, err := s.client.RebindConnection(as(t, testOperator), &pb.RebindConnectionRequest{
		ConnectionId: id.String(), ExpectedVersion: 1, Identity: identityMessage(callerIdentity),
	})
	if err != nil {
		t.Fatal(err)
	}
	got := rebound.GetConnection()
	if got.GetIdentityBindingRequired() || got.GetVersion() != 2 || got.GetLegacyBoundCaller().GetName() != legacy.Name ||
		got.GetGrants()[0].GetId() != grantID.String() || got.GetGrants()[0].GetCaller().GetName() != legacy.Name {
		t.Fatalf("rebind did not preserve history and clear pause: %+v", got)
	}
	tx, err = s.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	resolved, err := s.store.ResolveGrant(t.Context(), tx, grantID, "https://test-actions.example", "legacy-client")
	if err != nil || resolved.Identity != callerIdentity {
		t.Errorf("explicit rebind resolved %v, %v; want %v", resolved, err, callerIdentity)
	}
	_ = tx.Rollback()
}

func TestAnUnbindRevokesEveryGrantOnce(t *testing.T) {
	s := serve(t)
	created := now().Add(-time.Hour)
	id := s.connect(t, "test-unbound", created, &callerIdentity,
		storedGrant{identity: &otherIdentity, status: grantRevoked}, storedGrant{identity: &callerIdentity, status: grantActive})
	unbind := func(version int64) (*pb.UnbindConnectionResponse, error) {
		return s.client.UnbindConnection(as(t, testOperator), &pb.UnbindConnectionRequest{
			ConnectionId: id.String(), ExpectedVersion: version,
		})
	}
	unbound, err := unbind(1)
	if err != nil {
		t.Fatal(err)
	}
	got := unbound.GetConnection()
	wantBound(t, "the unbound Connection", got, nil)
	grants := got.GetGrants()
	if got.GetVersion() != 2 || len(grants) != 2 ||
		grants[0].GetStatus() != pb.GrantStatus_GRANT_STATUS_REVOKED ||
		!grants[0].GetRevokedAt().AsTime().Equal(created.Add(time.Minute)) ||
		grants[1].GetStatus() != pb.GrantStatus_GRANT_STATUS_REVOKED || grants[1].GetRevokedAt() == nil {
		t.Errorf("unbound %v", got)
	}
	// Repeating it, even at the version it was reviewed at, answers the Connection as it is.
	again, err := unbind(1)
	if err != nil || again.GetConnection().GetVersion() != 2 {
		t.Errorf("a repeated unbind: %v, %v", again, err)
	}

	live := s.active(t, "test-live", callerIdentity)
	_, err = s.client.UnbindConnection(as(t, testOperator), &pb.UnbindConnectionRequest{
		ConnectionId: live.GetId(), ExpectedVersion: 2,
	})
	wantCode(t, "an unbind at a stale version", err, codes.Aborted)
}

func TestConnectionIdentitiesAreListedOnlyFromACurrentView(t *testing.T) {
	s := serve(t)
	listed, err := s.client.ListConnectionIdentities(as(t, testOperator), &pb.ListConnectionIdentitiesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	got := listed.GetIdentities()
	if len(got) != 2 || got[0].GetName() != callerIdentity.Name || got[0].GetUid() != callerIdentity.UID ||
		got[1].GetName() != otherIdentity.Name || got[1].GetUid() != otherIdentity.UID {
		t.Errorf("listed %v", got)
	}
	s.identities.goStale()
	_, err = s.client.ListConnectionIdentities(as(t, testOperator), &pb.ListConnectionIdentitiesRequest{})
	wantCode(t, "a listing from a stale view", err, codes.Unavailable)
}
