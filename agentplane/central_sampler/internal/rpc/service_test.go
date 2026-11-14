package rpc

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestAdmissionAuthenticatesAndAuthorizesAccountUID(t *testing.T) {
	for _, c := range []struct {
		name, uid, account string
		headers            []string
		failure            error
		code               codes.Code
	}{
		{name: "accepted", uid: "test-account-uid", account: "system:serviceaccount:test:client", headers: []string{"Bearer test-token"}, code: codes.OK},
		{name: "missing bearer", code: codes.Unauthenticated},
		{name: "duplicate bearer", headers: []string{"Bearer test-token", "Bearer test-token"}, code: codes.Unauthenticated},
		{name: "invalid token", headers: []string{"Bearer invalid"}, code: codes.Unauthenticated},
		{name: "missing UID", account: "system:serviceaccount:test:client", headers: []string{"Bearer test-token"}, code: codes.Unauthenticated},
		{name: "account forbidden", uid: "test-account-uid", account: "system:serviceaccount:test:other", headers: []string{"Bearer test-token"}, code: codes.PermissionDenied},
		{name: "backend unavailable", headers: []string{"Bearer test-token"}, failure: errors.New("secret upstream token"), code: codes.Unavailable},
	} {
		t.Run(c.name, func(t *testing.T) {
			resolver, err := auth.NewResolver(fixtureReviews{c.uid, c.account, c.failure}, "test-sampler", []string{"test"})
			if err != nil {
				t.Fatal(err)
			}
			sampler := Sampler{Resolver: resolver, Settings: fixtureSettings()}
			ctx := metadata.NewIncomingContext(t.Context(), metadata.MD{"authorization": c.headers})
			_, cancel, principal, err := sampler.admitted(ctx)
			defer cancel()
			if status.Code(err) != c.code {
				t.Fatalf("code=%v expected %v", status.Code(err), c.code)
			}
			if err == nil && principal != c.uid {
				t.Fatalf("account UID %q", principal)
			}
			if err != nil && status.Convert(err).Message() == "secret upstream token" {
				t.Fatal("backend detail leaked")
			}
		})
	}
}

func TestSamplerErrorsExposeFixedStatus(t *testing.T) {
	for _, c := range []struct {
		err  error
		code codes.Code
	}{{sessionstore.ErrForbidden, codes.PermissionDenied}, {sessionstore.ErrConflict, codes.AlreadyExists}, {protocol.ErrMissing, codes.NotFound}, {protocol.ErrPrecondition, codes.FailedPrecondition}, {protocol.ErrInvalid, codes.InvalidArgument}, {errors.New("secret provider key"), codes.Unavailable}} {
		err := samplerError(t.Context(), c.err)
		if status.Code(err) != c.code {
			t.Fatalf("%v: %v", c.err, err)
		}
		if status.Convert(err).Message() == c.err.Error() {
			t.Fatalf("error detail leaked: %v", err)
		}
	}
}

func TestServiceAdmissionReturnsServiceOwnershipWithoutTokenReview(t *testing.T) {
	ca := transporttesting.NewCA(t)
	for _, tc := range []struct {
		name, actor string
		bearer      bool
		want        codes.Code
	}{
		{name: "Sessions role", actor: peertls.SessionsURI, want: codes.OK},
		{name: "former combined identity", actor: peertls.SessionDispatcherURI, want: codes.PermissionDenied},
		{name: "unrelated service", actor: peertls.ActionsURI, want: codes.PermissionDenied},
		{name: "bearer beside certificate", actor: peertls.SessionsURI, bearer: true, want: codes.Unauthenticated},
	} {
		t.Run(tc.name, func(t *testing.T) {
			files := ca.Issue(t, tc.actor)
			certificate, err := tls.LoadX509KeyPair(files.CertFile, files.KeyFile)
			if err != nil {
				t.Fatal(err)
			}
			leaf, err := x509.ParseCertificate(certificate.Certificate[0])
			if err != nil {
				t.Fatal(err)
			}
			roots := x509.NewCertPool()
			raw, err := os.ReadFile(ca.RootFile)
			if err != nil || !roots.AppendCertsFromPEM(raw) {
				t.Fatalf("roots: %v", err)
			}
			chains, err := leaf.Verify(x509.VerifyOptions{Roots: roots, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}})
			if err != nil {
				t.Fatal(err)
			}
			ctx := peer.NewContext(t.Context(), &peer.Peer{AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{PeerCertificates: []*x509.Certificate{leaf}, VerifiedChains: chains}}})
			if tc.bearer {
				ctx = metadata.NewIncomingContext(ctx, metadata.Pairs("authorization", "Bearer test-token"))
			}
			resolver, err := auth.NewResolver(fixtureReviews{failure: errors.New("unexpected TokenReview fallback")}, "test-sampler", []string{"test"})
			if err != nil {
				t.Fatal(err)
			}
			sampler := Sampler{Resolver: resolver, Settings: fixtureSettings()}
			sampler.Settings.ServiceCallers = []string{peertls.SessionsURI}
			admitted, cancel, owner, err := sampler.admitted(ctx)
			defer cancel()
			if status.Code(err) != tc.want {
				t.Fatalf("admission=%v,want=%s", err, tc.want)
			}
			if tc.want == codes.OK {
				if owner != peertls.SessionsURI {
					t.Fatalf("owner=%q", owner)
				}
				if _, err := auth.Caller(admitted); err == nil {
					t.Fatal("service obtained account proof")
				}
			}
		})
	}
}
