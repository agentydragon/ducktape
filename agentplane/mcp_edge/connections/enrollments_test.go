package connections

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/types/known/emptypb"
)

const (
	testBrowser      = "test-browser-binding-0123456789abcdef"
	testOtherBrowser = "test-other-browser-binding-0123456789"
	testUpstreamURL  = "https://test-actions.example/authorize/continue"
)

// sha256Hex is how the authorization server stores a handle, and how both services store a browser binding.
func sha256Hex(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

// enroll stores an enrollment as the authorization server creates one, and returns its handle.
func (s *served) enroll(t *testing.T, expiresAt time.Time) string {
	t.Helper()
	handle := "test-handle-" + uuid.NewString()
	if _, err := s.db.ExecContext(t.Context(), `
		INSERT INTO connection_enrollment (id, handle_hash, issuer, client_id, client_name, redirect_uri, code_challenge,
			upstream_url, expires_at, version)
		VALUES ($1, $2, 'https://test-actions.example', 'test-client', 'Test client',
			'https://test-client.example/callback', $3, $4, $5, 1)`,
		uuid.New(), sha256Hex(handle), "test-challenge-"+uuid.NewString(), testUpstreamURL, expiresAt); err != nil {
		t.Fatal(err)
	}
	return handle
}

func (s *served) preview(t *testing.T, subject, handle, browser string) (*pb.PreviewConnectionEnrollmentResponse, error) {
	t.Helper()
	return s.client.PreviewConnectionEnrollment(as(t, subject), &pb.PreviewConnectionEnrollmentRequest{
		Handle: handle, BrowserBinding: browser,
	})
}

// previewed is an enrollment testOperator previewed from testBrowser.
func (s *served) previewed(t *testing.T) string {
	t.Helper()
	handle := s.enroll(t, time.Now().Add(15*time.Minute))
	if _, err := s.preview(t, testOperator, handle, testBrowser); err != nil {
		t.Fatal(err)
	}
	return handle
}

func allowNew(handle string) *pb.DecideConnectionEnrollmentRequest {
	return &pb.DecideConnectionEnrollmentRequest{
		Handle: handle, BrowserBinding: testBrowser, ExpectedVersion: 1,
		Decision: &pb.DecideConnectionEnrollmentRequest_Allow{Allow: &pb.EnrollmentAllow{
			ServiceAccount: message(callerAccount),
			Connection: &pb.EnrollmentAllow_NewConnection{
				NewConnection: &pb.NewConnection{DisplayName: " test-new-connection "},
			},
		}},
	}
}

func deny(handle string) *pb.DecideConnectionEnrollmentRequest {
	return &pb.DecideConnectionEnrollmentRequest{
		Handle: handle, BrowserBinding: testBrowser, ExpectedVersion: 1,
		Decision: &pb.DecideConnectionEnrollmentRequest_Deny{Deny: &emptypb.Empty{}},
	}
}

func reconnect(handle, id string, version int64) *pb.DecideConnectionEnrollmentRequest {
	request := allowNew(handle)
	request.GetAllow().Connection = &pb.EnrollmentAllow_Reconnect{Reconnect: &pb.ReconnectConnection{
		ConnectionId: id, ExpectedVersion: version, AuthorityChangeConfirmed: true,
	}}
	return request
}

// storedDecision is the decision an enrollment row records.
type storedDecision struct {
	verdict, displayName, connectionID sql.NullString
	// callerNamespace and callerName are those of the caller document; NULL when there is none.
	callerNamespace, callerName sql.NullString
	version                     int64
	connectionVersion           sql.NullInt64
}

func (s *served) storedDecision(t *testing.T, handle string) storedDecision {
	t.Helper()
	var d storedDecision
	if err := s.db.QueryRowContext(
		t.Context(), `
		SELECT verdict, caller->>'namespace', caller->>'name', display_name, connection_id::text, version,
			connection_version
		FROM connection_enrollment WHERE handle_hash = $1`, sha256Hex(handle),
	).Scan(
		&d.verdict, &d.callerNamespace, &d.callerName, &d.displayName, &d.connectionID, &d.version, &d.connectionVersion,
	); err != nil {
		t.Fatal(err)
	}
	return d
}

func TestTheFirstPreviewBindsAnEnrollmentToItsBrowserAndOperator(t *testing.T) {
	s := serve(t)
	expiresAt := now().Add(15 * time.Minute)
	handle := s.enroll(t, expiresAt)
	got, err := s.preview(t, testOperator, handle, testBrowser)
	if err != nil {
		t.Fatal(err)
	}
	if got.GetClientId() != "test-client" || got.GetClientName() != "Test client" ||
		got.GetRedirectUri() != "https://test-client.example/callback" || !got.GetExpiresAt().AsTime().Equal(expiresAt) ||
		got.GetVersion() != 1 {
		t.Errorf("preview %v", got)
	}
	// The authorization server reads the binding back as these columns.
	var browserHash, issuer, subject string
	if err := s.db.QueryRowContext(t.Context(), `
		SELECT browser_hash, operator_issuer, operator_subject FROM connection_enrollment WHERE handle_hash = $1`,
		sha256Hex(handle)).Scan(&browserHash, &issuer, &subject); err != nil {
		t.Fatal(err)
	}
	if browserHash != sha256Hex(testBrowser) || issuer != testIssuer || subject != testOperator {
		t.Errorf("bound to %s, %s, %s", browserHash, issuer, subject)
	}
	if _, err := s.preview(t, testOperator, handle, testBrowser); err != nil {
		t.Errorf("a second preview from the bound browser: %v", err)
	}
	_, err = s.preview(t, testOperator, handle, testOtherBrowser)
	wantCode(t, "a preview from another browser", err, codes.PermissionDenied)
	_, err = s.preview(t, testOtherOperator, handle, testBrowser)
	wantCode(t, "a preview by another operator", err, codes.PermissionDenied)
	_, err = s.client.DecideConnectionEnrollment(as(t, testOtherOperator), deny(handle))
	wantCode(t, "a decision by another operator", err, codes.PermissionDenied)

	// Only a preview binds an enrollment: a decision on one nobody previewed is refused.
	_, err = s.client.DecideConnectionEnrollment(as(t, testOperator), deny(s.enroll(t, expiresAt)))
	wantCode(t, "a decision before any preview", err, codes.PermissionDenied)
}

func TestAPreviewRefusesAnUnknownOrExpiredEnrollmentAndAMalformedBinding(t *testing.T) {
	s := serve(t)
	_, err := s.preview(t, testOperator, "test-unknown-handle", testBrowser)
	wantCode(t, "an unknown handle", err, codes.NotFound)
	_, err = s.preview(t, testOperator, s.enroll(t, time.Now().Add(-time.Second)), testBrowser)
	wantCode(t, "an expired enrollment", err, codes.FailedPrecondition)
	_, err = s.preview(t, testOperator, s.enroll(t, time.Now().Add(time.Minute)), strings.Repeat("b", 31))
	wantCode(t, "a binding of 31 characters", err, codes.InvalidArgument)
}

func TestAnAllowIsRecordedOnceAndAnExactRetryGetsItsAnswer(t *testing.T) {
	s := serve(t)
	handle := s.previewed(t)
	const decisions = 4
	answers := make([]*pb.DecideConnectionEnrollmentResponse, decisions)
	failures := make([]error, decisions)
	var wg sync.WaitGroup
	for i := range decisions {
		wg.Go(func() {
			answers[i], failures[i] = s.client.DecideConnectionEnrollment(as(t, testOperator), allowNew(handle))
		})
	}
	wg.Wait()
	for i, answer := range answers {
		if failures[i] != nil || answer.GetVerdict() != action_servicepb.Verdict_VERDICT_ALLOW ||
			answer.GetRedirectUrl() != testUpstreamURL {
			t.Errorf("decision %d: %v, %v", i, answer, failures[i])
		}
	}
	got := s.storedDecision(t, handle)
	if got.verdict.String != "allow" || got.callerNamespace.String != callerAccount.Namespace ||
		got.callerName.String != callerAccount.Name ||
		got.displayName.String != "test-new-connection" || got.connectionID.Valid || got.version != 2 {
		t.Errorf("recorded %+v", got)
	}
	// An allow creates no Connection: the authorization server does, once the client exchanges its code.
	var connections int
	if err := s.db.QueryRowContext(t.Context(), `SELECT count(*) FROM external_connection`).Scan(&connections); err != nil {
		t.Fatal(err)
	}
	if connections != 0 {
		t.Errorf("%d Connections after an allow", connections)
	}

	_, err := s.client.DecideConnectionEnrollment(as(t, testOperator), deny(handle))
	wantCode(t, "a deny after the allow", err, codes.FailedPrecondition)
	other := allowNew(handle)
	other.GetAllow().GetNewConnection().DisplayName = "test-other-connection"
	_, err = s.client.DecideConnectionEnrollment(as(t, testOperator), other)
	wantCode(t, "an allow of another Connection after the allow", err, codes.FailedPrecondition)
}

func TestADenyRecordsNoAccountAndSendsTheBrowserNowhere(t *testing.T) {
	s := serve(t)
	handle := s.previewed(t)
	answer, err := s.client.DecideConnectionEnrollment(as(t, testOperator), deny(handle))
	if err != nil {
		t.Fatal(err)
	}
	if answer.GetVerdict() != action_servicepb.Verdict_VERDICT_DENY || answer.RedirectUrl != nil {
		t.Errorf("answer %v", answer)
	}
	if got := s.storedDecision(t, handle); got.verdict.String != "deny" || got.callerName.Valid || got.version != 2 {
		t.Errorf("recorded %+v", got)
	}
}

func TestADecisionNeedsTheReviewedVersionAndALabeledAccount(t *testing.T) {
	s := serve(t)
	handle := s.previewed(t)
	stale := deny(handle)
	stale.ExpectedVersion = 2
	_, err := s.client.DecideConnectionEnrollment(as(t, testOperator), stale)
	wantCode(t, "a decision at a stale version", err, codes.Aborted)
	unlabeled := allowNew(handle)
	unlabeled.GetAllow().ServiceAccount = message(unlabeledAccount)
	_, err = s.client.DecideConnectionEnrollment(as(t, testOperator), unlabeled)
	wantCode(t, "an allow of an unlabeled account", err, codes.PermissionDenied)
	s.callers.goStale()
	_, err = s.client.DecideConnectionEnrollment(as(t, testOperator), allowNew(handle))
	wantCode(t, "an allow while the view of the callers is stale", err, codes.PermissionDenied)
	// None of them was recorded.
	if got := s.storedDecision(t, handle); got.verdict.Valid || got.version != 1 {
		t.Errorf("recorded %+v", got)
	}
}

func TestAMalformedDecisionIsAnInvalidArgument(t *testing.T) {
	s := serve(t)
	handle := s.previewed(t)
	for name, change := range map[string]func(*pb.DecideConnectionEnrollmentRequest){
		"no decision":               func(r *pb.DecideConnectionEnrollmentRequest) { r.Decision = nil },
		"a short browser binding":   func(r *pb.DecideConnectionEnrollmentRequest) { r.BrowserBinding = "test-short" },
		"version 0":                 func(r *pb.DecideConnectionEnrollmentRequest) { r.ExpectedVersion = 0 },
		"an allow of no account":    func(r *pb.DecideConnectionEnrollmentRequest) { r.GetAllow().ServiceAccount = nil },
		"an allow of no Connection": func(r *pb.DecideConnectionEnrollmentRequest) { r.GetAllow().Connection = nil },
		"a blank Connection name": func(r *pb.DecideConnectionEnrollmentRequest) {
			r.GetAllow().GetNewConnection().DisplayName = " "
		},
		"an unconfirmed reconnect": func(r *pb.DecideConnectionEnrollmentRequest) {
			r.GetAllow().Connection = &pb.EnrollmentAllow_Reconnect{Reconnect: &pb.ReconnectConnection{
				ConnectionId: uuid.NewString(), ExpectedVersion: 1,
			}}
		},
		"a reconnect of a malformed id": func(r *pb.DecideConnectionEnrollmentRequest) {
			r.GetAllow().Connection = &pb.EnrollmentAllow_Reconnect{Reconnect: &pb.ReconnectConnection{
				ConnectionId: "test-not-a-uuid", ExpectedVersion: 1, AuthorityChangeConfirmed: true,
			}}
		},
	} {
		request := allowNew(handle)
		change(request)
		_, err := s.client.DecideConnectionEnrollment(as(t, testOperator), request)
		wantCode(t, name, err, codes.InvalidArgument)
	}
}

func TestAReconnectRecordsTheConnectionAtTheVersionReviewed(t *testing.T) {
	s := serve(t)
	c := s.active(t, "test-reconnected", callerAccount)
	handle := s.previewed(t)
	answer, err := s.client.DecideConnectionEnrollment(as(t, testOperator), reconnect(handle, c.GetId(), 1))
	if err != nil || answer.GetVerdict() != action_servicepb.Verdict_VERDICT_ALLOW {
		t.Fatalf("reconnect: %v, %v", answer, err)
	}
	if got := s.storedDecision(t, handle); got.connectionID.String != c.GetId() || got.connectionVersion.Int64 != 1 ||
		got.displayName.Valid {
		t.Errorf("recorded %+v", got)
	}

	if _, err := s.client.UpdateConnection(as(t, testOperator), &pb.UpdateConnectionRequest{
		ConnectionId: c.GetId(), ExpectedVersion: 1, DisplayName: "test-changed",
	}); err != nil {
		t.Fatal(err)
	}
	_, err = s.client.DecideConnectionEnrollment(as(t, testOperator), reconnect(s.previewed(t), c.GetId(), 1))
	wantCode(t, "a reconnect of a Connection changed since review", err, codes.Aborted)
	_, err = s.client.DecideConnectionEnrollment(as(t, testOperator), reconnect(s.previewed(t), uuid.NewString(), 1))
	wantCode(t, "a reconnect of an unknown Connection", err, codes.NotFound)
}
