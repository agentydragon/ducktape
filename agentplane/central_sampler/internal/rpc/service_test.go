package rpc

import (
	"errors"
	"testing"

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
			if err == nil && principal.ServiceAccountUID != c.uid {
				t.Fatalf("account UID %q", principal.ServiceAccountUID)
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
