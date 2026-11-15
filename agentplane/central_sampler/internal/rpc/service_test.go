package rpc

import (
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestAdmissionRequiresAllowedServiceSVID(t *testing.T) {
	for _, tc := range []struct {
		name, actor string
		bearer      bool
		noPeer      bool
		want        codes.Code
	}{
		{name: "accepted service", actor: peertls.SessionsURI, want: codes.OK},
		{name: "unlisted service", actor: peertls.ActionsURI, want: codes.PermissionDenied},
		{name: "former combined service role", actor: peertls.SessionDispatcherURI, want: codes.PermissionDenied},
		{name: "bearer beside certificate", actor: peertls.SessionsURI, bearer: true, want: codes.Unauthenticated},
		{name: "bearer without certificate", bearer: true, noPeer: true, want: codes.Unauthenticated},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx := t.Context()
			if !tc.noPeer {
				ctx = serviceContext(t, tc.actor)
			}
			if tc.bearer {
				ctx = metadata.NewIncomingContext(ctx, metadata.Pairs("authorization", "Bearer test-token"))
			}
			settings := fixtureSettings()
			settings.ServiceCallers = []string{peertls.SessionsURI}
			sampler := Sampler{Settings: settings}
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
