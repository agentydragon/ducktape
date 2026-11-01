package connections

import (
	"strings"
	"sync"
	"testing"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// accountOf is the account a ServiceAccount message names; nil names none.
func accountOf(account *sandboxes.ServiceAccount) *auth.Account {
	if account == nil {
		return nil
	}
	return &auth.Account{Namespace: account.GetNamespace(), Name: account.GetName()}
}

func wantBound(t *testing.T, what string, c *pb.Connection, want *auth.Account) {
	t.Helper()
	got := accountOf(c.GetBoundCaller())
	if (got == nil) != (want == nil) || got != nil && *got != *want {
		t.Errorf("%s: bound to %v, want %v", what, got, want)
	}
}

func message(account auth.Account) *sandboxes.ServiceAccount {
	return &sandboxes.ServiceAccount{Namespace: account.Namespace, Name: account.Name}
}

func TestConnectionsAreListedOldestFirstWithEveryGrantInRevisionOrder(t *testing.T) {
	s := serve(t)
	start := now().Add(-time.Hour)
	newer := s.connect(t, "test-newer", start.Add(time.Minute), &callerAccount,
		storedGrant{caller: otherAccount, status: grantRevoked}, storedGrant{caller: callerAccount, status: grantActive})
	older := s.connect(t, "test-older", start, nil, storedGrant{caller: otherAccount, status: grantRevoked})
	listed, err := s.client.ListConnections(as(t, testOperator), &pb.ListConnectionsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	got := listed.GetConnections()
	if len(got) != 2 || got[0].GetId() != older.String() || got[1].GetId() != newer.String() {
		t.Fatalf("listed %v, want %s then %s", got, older, newer)
	}
	wantBound(t, "the unbound Connection", got[0], nil)
	wantBound(t, "the bound Connection", got[1], &callerAccount)
	grants := got[1].GetGrants()
	if len(grants) != 2 ||
		grants[0].GetRevision() != 1 || grants[0].GetStatus() != pb.GrantStatus_GRANT_STATUS_REVOKED ||
		*accountOf(grants[0].GetCaller()) != otherAccount || grants[0].GetRevokedAt() == nil ||
		grants[1].GetRevision() != 2 || grants[1].GetStatus() != pb.GrantStatus_GRANT_STATUS_ACTIVE ||
		grants[1].GetActivatedAt() == nil || grants[1].GetRevokedAt() != nil {
		t.Errorf("grants %v", grants)
	}

	_, err = s.client.GetConnection(as(t, testOperator), &pb.GetConnectionRequest{ConnectionId: uuid.NewString()})
	wantCode(t, "get an unknown Connection", err, codes.NotFound)
	_, err = s.client.GetConnection(as(t, testOperator), &pb.GetConnectionRequest{ConnectionId: "test-not-a-uuid"})
	wantCode(t, "get by a malformed id", err, codes.InvalidArgument)
}

func TestARenameTrimsTheNameAndNeedsTheCurrentVersion(t *testing.T) {
	s := serve(t)
	c := s.active(t, "test-named", callerAccount)
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
	c := s.active(t, "test-contested", callerAccount)
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
	c := s.active(t, "test-rebound", callerAccount)
	rebind := func(id string, version int64, account auth.Account) (*pb.RebindConnectionResponse, error) {
		return s.client.RebindConnection(as(t, testOperator), &pb.RebindConnectionRequest{
			ConnectionId: id, ExpectedVersion: version, ServiceAccount: message(account),
		})
	}
	rebound, err := rebind(c.GetId(), 1, otherAccount)
	if err != nil {
		t.Fatal(err)
	}
	got := rebound.GetConnection()
	wantBound(t, "the rebound Connection", got, &otherAccount)
	// The grant keeps the account consent chose.
	if got.GetVersion() != 2 || *accountOf(got.GetGrants()[0].GetCaller()) != callerAccount {
		t.Errorf("rebound %v", got)
	}
	same, err := rebind(c.GetId(), 2, otherAccount)
	if err != nil || same.GetConnection().GetVersion() != 2 {
		t.Errorf("a rebind to the bound account: %v, %v; want it unchanged", same, err)
	}
	_, err = rebind(c.GetId(), 1, callerAccount)
	wantCode(t, "a rebind at a stale version", err, codes.Aborted)
	_, err = rebind(c.GetId(), 2, unlabeledAccount)
	wantCode(t, "a rebind to an unlabeled account", err, codes.FailedPrecondition)
	_, err = s.client.RebindConnection(as(t, testOperator), &pb.RebindConnectionRequest{
		ConnectionId: c.GetId(), ExpectedVersion: 2, ServiceAccount: &sandboxes.ServiceAccount{Namespace: "test-agentplane"},
	})
	wantCode(t, "a rebind to an unnamed account", err, codes.InvalidArgument)

	pending := s.connect(t, "test-pending", now(), &callerAccount, storedGrant{caller: callerAccount, status: grantPending})
	_, err = rebind(pending.String(), 1, otherAccount)
	wantCode(t, "a rebind of a Connection with only a pending grant", err, codes.FailedPrecondition)
	unbound := s.connect(t, "test-unbound", now(), nil, storedGrant{caller: callerAccount, status: grantRevoked})
	_, err = rebind(unbound.String(), 1, otherAccount)
	wantCode(t, "a rebind of an unbound Connection", err, codes.FailedPrecondition)

	// A view of the callers that is not current vouches for no account.
	s.callers.goStale()
	_, err = rebind(c.GetId(), 2, callerAccount)
	wantCode(t, "a rebind while the view of the callers is stale", err, codes.FailedPrecondition)
}

func TestAnUnbindRevokesEveryGrantOnce(t *testing.T) {
	s := serve(t)
	created := now().Add(-time.Hour)
	id := s.connect(t, "test-unbound", created, &callerAccount,
		storedGrant{caller: otherAccount, status: grantRevoked}, storedGrant{caller: callerAccount, status: grantActive})
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

	live := s.active(t, "test-live", callerAccount)
	_, err = s.client.UnbindConnection(as(t, testOperator), &pb.UnbindConnectionRequest{
		ConnectionId: live.GetId(), ExpectedVersion: 2,
	})
	wantCode(t, "an unbind at a stale version", err, codes.Aborted)
}

func TestTheCallerAccountsAreListedOnlyFromACurrentView(t *testing.T) {
	s := serve(t)
	listed, err := s.client.ListCallerServiceAccounts(as(t, testOperator), &pb.ListCallerServiceAccountsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	var got []auth.Account
	for _, account := range listed.GetServiceAccounts() {
		got = append(got, *accountOf(account))
	}
	if len(got) != 2 || got[0] != callerAccount || got[1] != otherAccount {
		t.Errorf("listed %v", got)
	}
	s.callers.goStale()
	_, err = s.client.ListCallerServiceAccounts(as(t, testOperator), &pb.ListCallerServiceAccountsRequest{})
	wantCode(t, "a listing from a stale view", err, codes.Unavailable)
}
