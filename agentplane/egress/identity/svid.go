package identity

import (
	"context"
	"crypto/tls"
	"crypto/x509"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
)

// ResolveSVID rechecks the exact live Sandbox credential and Pod binding for every
// request. Only the Sandbox supplies the ServiceAccount used as the policy subject.
func ResolveSVID(ctx context.Context, state *tls.ConnectionState, readers map[string]*sandboxreader.Reader) (auth.Principal, error) {
	if state == nil {
		return auth.Principal{}, &Rejection{Reason: decision.TokenRejected, Detail: "invalid SVID peer certificate"}
	}
	peerURI, err := environment_runtime_transport.PeerIdentityFromTLSState(*state, x509.ExtKeyUsageClientAuth)
	if err != nil {
		return auth.Principal{}, &Rejection{Reason: decision.TokenRejected, Detail: "invalid SVID peer certificate"}
	}
	namespace, sandboxUID, credentialID, err := environment_runtime_transport.ParseEnvironmentRuntimeURI(peerURI)
	if err != nil {
		return auth.Principal{}, &Rejection{Reason: decision.TokenRejected, Detail: "invalid SVID identity URI"}
	}
	reader := readers[namespace]
	if reader == nil {
		return auth.Principal{}, &Rejection{Reason: decision.Unavailable, Detail: "SVID authentication is not configured"}
	}
	view, found, err := reader.FindRunnerCredential(ctx, sandboxUID, credentialID)
	if err != nil {
		return auth.Principal{}, &Rejection{Reason: decision.Unavailable, Detail: "live Sandbox lookup failed"}
	}
	if !found || view.Identity == nil || view.Resource == nil {
		return auth.Principal{}, &Rejection{Reason: decision.TokenRejected, Detail: "SVID does not name a current Sandbox credential"}
	}
	podName, podUID, bound := view.RunnerPod()
	if !bound || view.Identity.Namespace != namespace || view.Identity.Name != view.Resource.Name || view.Resource.Name == "" {
		return auth.Principal{}, &Rejection{Reason: decision.TokenRejected, Detail: "Sandbox has no current runner binding"}
	}
	return auth.Principal{
		Account: auth.Account{Namespace: view.Identity.Namespace, Name: view.Identity.Name},
		PodName: podName, PodUID: podUID,
	}, nil
}
