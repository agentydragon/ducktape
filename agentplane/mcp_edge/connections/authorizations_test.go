package connections

import (
	"database/sql"
	"errors"
	"testing"

	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
)

const (
	// testServerIssuer is the authorization server's issuer, which its grants name.
	testServerIssuer = "https://test-actions.example"
	testClient       = "test-client"
)

// approver is the operator whose consent the authorization server requires.
var approver = Operator{Issuer: testIssuer, Subject: testOperator}

func (s *served) createEnrollment(t *testing.T, e NewEnrollment) (string, error) {
	t.Helper()
	var handle string
	err := s.store.transaction(t.Context(), func(tx *sql.Tx) error {
		var err error
		handle, _, err = s.store.CreateEnrollment(t.Context(), tx, e)
		return err
	})
	return handle, err
}

// authorize creates an enrollment as the authorization server does and previews it as testOperator.
func (s *served) authorize(t *testing.T) (uuid.UUID, string) {
	t.Helper()
	id := uuid.New()
	handle, err := s.createEnrollment(t, NewEnrollment{
		ID: id, Issuer: testServerIssuer, ClientID: testClient, RedirectURI: "https://test-client.example/callback",
		CodeChallenge: "test-challenge-" + id.String(), UpstreamURL: testUpstreamURL,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.preview(t, testOperator, handle); err != nil {
		t.Fatal(err)
	}
	return id, handle
}

func (s *served) decide(t *testing.T, request *pb.DecideConnectionEnrollmentRequest) {
	t.Helper()
	if _, err := s.client.DecideConnectionEnrollment(as(t, testOperator), request); err != nil {
		t.Fatal(err)
	}
}

// allowed is an authorization testOperator allowed to create a Connection bound to the account.
func (s *served) allowed(t *testing.T, account auth.Account) uuid.UUID {
	t.Helper()
	id, handle := s.authorize(t)
	request := allowNew(handle)
	request.GetAllow().ServiceAccount = message(account)
	s.decide(t, request)
	return id
}

func (s *served) activate(t *testing.T, id uuid.UUID, by Operator) error {
	t.Helper()
	return s.store.transaction(t.Context(), func(tx *sql.Tx) error { return s.store.Activate(t.Context(), tx, id, by) })
}

func (s *served) resolve(t *testing.T, id uuid.UUID, issuer, clientID string) (auth.Account, error) {
	t.Helper()
	var account auth.Account
	err := s.store.transaction(t.Context(), func(tx *sql.Tx) error {
		var err error
		account, err = s.store.ResolveGrant(t.Context(), tx, id, issuer, clientID)
		return err
	})
	return account, err
}

func (s *served) revoke(t *testing.T, id uuid.UUID) error {
	t.Helper()
	return s.store.transaction(t.Context(), func(tx *sql.Tx) error { return s.store.RevokeGrant(t.Context(), tx, id) })
}

// connectionOf is the Connection the grant belongs to.
func (s *served) connectionOf(t *testing.T, grant uuid.UUID) *pb.Connection {
	t.Helper()
	var id uuid.UUID
	if err := s.db.QueryRowContext(t.Context(), `SELECT connection_id FROM external_connection_grant WHERE id = $1`,
		grant).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return s.get(t, id)
}

func wantRefusal(t *testing.T, what string, err error, want Refusal) {
	t.Helper()
	var got Refusal
	if !errors.As(err, &got) || got != want {
		t.Errorf("%s: %v, want %q", what, err, want)
	}
}

func TestAnEnrollmentOfAnEarlierOnesClientRedirectAndChallengeIsRefused(t *testing.T) {
	s := serve(t)
	enrollment := NewEnrollment{
		ID: uuid.New(), Issuer: testServerIssuer, ClientID: testClient, RedirectURI: "https://test-client.example/callback",
		CodeChallenge: "test-challenge", UpstreamURL: testUpstreamURL,
	}
	if _, err := s.createEnrollment(t, enrollment); err != nil {
		t.Fatal(err)
	}
	enrollment.ID = uuid.New()
	_, err := s.createEnrollment(t, enrollment)
	wantRefusal(t, "a second enrollment of the challenge", err, refusedReused)
	enrollment.CodeChallenge = "test-other-challenge"
	if _, err := s.createEnrollment(t, enrollment); err != nil {
		t.Errorf("an enrollment of another challenge: %v", err)
	}
}

func TestActivatingAnAllowCreatesTheConnectionItNamedOnce(t *testing.T) {
	s := serve(t)
	id := s.allowed(t, callerAccount)
	if err := s.activate(t, id, approver); err != nil {
		t.Fatal(err)
	}
	c := s.connectionOf(t, id)
	wantBound(t, "the created Connection", c, &callerAccount)
	grants := c.GetGrants()
	if c.GetDisplayName() != "test-new-connection" || c.GetVersion() != 1 || len(grants) != 1 ||
		grants[0].GetId() != id.String() || grants[0].GetRevision() != 1 ||
		grants[0].GetStatus() != pb.GrantStatus_GRANT_STATUS_ACTIVE || *accountOf(grants[0].GetCaller()) != callerAccount ||
		grants[0].GetIssuer() != testServerIssuer || grants[0].GetClientId() != testClient ||
		grants[0].GetActivatedAt() == nil {
		t.Errorf("created %v", c)
	}
	wantRefusal(t, "a second activation", s.activate(t, id, approver), refusedClaimed)
}

func TestActivatingAReconnectReplacesTheConnectionsAuthorityInOneChange(t *testing.T) {
	s := serve(t)
	c := s.active(t, "test-reconnected", otherAccount)
	id, handle := s.authorize(t)
	s.decide(t, reconnect(handle, c.GetId(), 1))
	if err := s.activate(t, id, approver); err != nil {
		t.Fatal(err)
	}
	got := s.get(t, uuid.MustParse(c.GetId()))
	wantBound(t, "the reconnected Connection", got, &callerAccount)
	grants := got.GetGrants()
	if got.GetVersion() != 2 || len(grants) != 2 ||
		grants[0].GetStatus() != pb.GrantStatus_GRANT_STATUS_REVOKED || grants[0].GetRevokedAt() == nil ||
		grants[1].GetId() != id.String() || grants[1].GetRevision() != 2 ||
		grants[1].GetStatus() != pb.GrantStatus_GRANT_STATUS_ACTIVE {
		t.Errorf("reconnected %v", got)
	}
}

func TestActivationRefusesAConsentThatAllowsNoGrantNow(t *testing.T) {
	s := serve(t)
	undecided, _ := s.authorize(t)
	denied, handle := s.authorize(t)
	s.decide(t, deny(handle))
	expired := s.allowed(t, callerAccount)
	if _, err := s.db.ExecContext(t.Context(),
		`UPDATE connection_enrollment SET expires_at = now() - interval '1 second' WHERE id = $1`, expired); err != nil {
		t.Fatal(err)
	}
	unlabeled := s.allowed(t, otherAccount)
	s.callers.label(callerAccount)
	reconnected := s.active(t, "test-reconnected", callerAccount)
	changed, handle := s.authorize(t)
	s.decide(t, reconnect(handle, reconnected.GetId(), 1))
	s.rename(t, reconnected, "test-renamed")
	allowed := s.allowed(t, callerAccount)

	for _, refused := range []struct {
		what string
		id   uuid.UUID
		by   Operator
		want Refusal
	}{
		{"an unknown enrollment", uuid.New(), approver, refusedUnknown},
		{"an expired consent", expired, approver, refusedExpired},
		{"an undecided consent", undecided, approver, refusedNotApproved},
		{"a denied consent", denied, approver, refusedNotApproved},
		{"a consent another operator gave", allowed, Operator{Issuer: testIssuer, Subject: testOtherOperator}, refusedOperator},
		{"a consent another issuer's operator gave", allowed, Operator{Issuer: "https://test-other-issuer.example", Subject: testOperator}, refusedOperator},
		{"a consent for an account no longer labeled", unlabeled, approver, refusedCaller},
		{"a reconnect of a Connection changed since consent", changed, approver, refusedChanged},
	} {
		wantRefusal(t, refused.what, s.activate(t, refused.id, refused.by), refused.want)
	}
	var connections, claimed int
	if err := s.db.QueryRowContext(
		t.Context(), `
		SELECT (SELECT count(*) FROM external_connection),
			(SELECT count(*) FROM connection_enrollment WHERE exchange_claimed_at IS NOT NULL)`,
	).Scan(&connections, &claimed); err != nil {
		t.Fatal(err)
	}
	if connections != 1 || claimed != 0 {
		t.Errorf("%d Connections and %d claimed enrollments after the refusals", connections, claimed)
	}
	if got := s.get(t, uuid.MustParse(reconnected.GetId())); got.GetVersion() != 2 || len(got.GetGrants()) != 1 {
		t.Errorf("a refused reconnect changed %v", got)
	}
	if err := s.activate(t, allowed, approver); err != nil {
		t.Errorf("the approver activating the consent: %v", err)
	}
}

func TestAGrantResolvesToTheAccountItsConnectionIsBoundToNow(t *testing.T) {
	s := serve(t)
	id := s.allowed(t, callerAccount)
	if err := s.activate(t, id, approver); err != nil {
		t.Fatal(err)
	}
	if account, err := s.resolve(t, id, testServerIssuer, testClient); err != nil || account != callerAccount {
		t.Errorf("resolved %v, %v; want %v", account, err, callerAccount)
	}
	if _, err := s.client.RebindConnection(as(t, testOperator), &pb.RebindConnectionRequest{
		ConnectionId: s.connectionOf(t, id).GetId(), ExpectedVersion: 1, ServiceAccount: message(otherAccount),
	}); err != nil {
		t.Fatal(err)
	}
	if account, err := s.resolve(t, id, testServerIssuer, testClient); err != nil || account != otherAccount {
		t.Errorf("resolved %v, %v after the rebind; want %v", account, err, otherAccount)
	}
}

func TestAGrantResolvesOnlyWhileActiveForItsIssuerAndClientOnABoundLabeledConnection(t *testing.T) {
	s := serve(t)
	id := s.allowed(t, callerAccount)
	if err := s.activate(t, id, approver); err != nil {
		t.Fatal(err)
	}
	// Python could leave a Connection unbound with an active grant; an unbind revokes its grants.
	unbound := s.connect(t, "test-unbound", now(), nil, storedGrant{caller: callerAccount, status: grantActive})
	var unboundGrant uuid.UUID
	if err := s.db.QueryRowContext(t.Context(), `SELECT id FROM external_connection_grant WHERE connection_id = $1`,
		unbound).Scan(&unboundGrant); err != nil {
		t.Fatal(err)
	}
	for _, refused := range []struct {
		what             string
		id               uuid.UUID
		issuer, clientID string
		want             Refusal
	}{
		{"an unknown grant", uuid.New(), testServerIssuer, testClient, refusedGrant},
		{"another issuer's grant", id, "https://test-other-actions.example", testClient, refusedGrant},
		{"another client's grant", id, testServerIssuer, "test-other-client", refusedGrant},
		{"the grant of an unbound Connection", unboundGrant, testServerIssuer, "test-client-1", refusedUnbound},
	} {
		_, err := s.resolve(t, refused.id, refused.issuer, refused.clientID)
		wantRefusal(t, refused.what, err, refused.want)
	}

	s.callers.label(otherAccount)
	_, err := s.resolve(t, id, testServerIssuer, testClient)
	wantRefusal(t, "a grant whose account is no longer labeled", err, refusedCaller)
	s.callers.label(callerAccount)
	if err := s.revoke(t, id); err != nil {
		t.Fatal(err)
	}
	_, err = s.resolve(t, id, testServerIssuer, testClient)
	wantRefusal(t, "a revoked grant", err, refusedGrant)
}

func TestRevokingAGrantChangesItsConnectionOnceAndKeepsItBound(t *testing.T) {
	s := serve(t)
	id := s.allowed(t, callerAccount)
	if err := s.activate(t, id, approver); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := s.revoke(t, id); err != nil {
			t.Fatal(err)
		}
	}
	got := s.connectionOf(t, id)
	wantBound(t, "the Connection of the revoked grant", got, &callerAccount)
	if got.GetVersion() != 2 || got.GetGrants()[0].GetStatus() != pb.GrantStatus_GRANT_STATUS_REVOKED ||
		got.GetGrants()[0].GetRevokedAt() == nil {
		t.Errorf("revoked %v", got)
	}
	if err := s.revoke(t, uuid.New()); err != nil {
		t.Errorf("revoking an unknown grant: %v", err)
	}
}
