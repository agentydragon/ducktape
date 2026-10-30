// Package informer keeps a policy.Index equal to the API server and reports the EgressReady
// condition this proxy keeps on each PolicyBinding: the one write the proxy makes to the API server.
package informer

import (
	"context"
	"errors"
	"log/slog"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	egressclient "github.com/agentydragon/ducktape/agentplane/egress/client/clientset/versioned"
	egressinformers "github.com/agentydragon/ducktape/agentplane/egress/client/informers/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	corev1 "k8s.io/api/core/v1"
	coreinformers "k8s.io/client-go/informers/core/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

// EgressReady is the condition this proxy keeps on each PolicyBinding.
var EgressReady = reader.Condition{Type: "EgressReady", FieldManager: "agentplane-egress", Rules: "egress rules", Reason: "Enforced"}

// Config says what to watch.
type Config struct {
	Index *policy.Index
	// RulesNamespace holds the PolicySets, PolicyBindings and EgressCredentials this proxy enforces.
	RulesNamespace string
	// CredentialsNamespace holds the Secrets those credentials read.
	CredentialsNamespace string
}

// Informer fills the Index from the API server and keeps EgressReady on the bindings.
type Informer struct {
	index   *policy.Index
	watches *reader.Watches
}

// New returns an Informer that is idle until Run. The API server is reached only from Run.
func New(base *rest.Config, config Config) (*Informer, error) {
	watches := reader.NewWatches(base, config.Index.Health)
	credentials, err := egressclient.NewForConfig(watches.Config())
	if err != nil {
		return nil, err
	}
	core, err := kubernetes.NewForConfig(watches.Config())
	if err != nil {
		return nil, err
	}
	i := &Informer{index: config.Index, watches: watches}
	if err := errors.Join(
		reader.WatchRules(watches, config.Index.Rules, config.RulesNamespace, EgressReady),
		reader.Watch(watches, policy.Credentials, egressinformers.NewTypedFilteredEgressCredentialInformer(credentials, config.RulesNamespace, 0, nil, nil), i.putCredential, i.index.DeleteCredential),
		reader.Watch(watches, policy.Secrets, coreinformers.NewTypedFilteredSecretInformer(core, config.CredentialsNamespace, 0, nil, nil), i.putSecret, i.index.DeleteSecret),
	); err != nil {
		return nil, err
	}
	return i, nil
}

// putCredential removes a credential this proxy cannot read, rather than keep the last one it could:
// a credential read wrongly is a placeholder forwarded raw or a secret sent where it should not go.
func (i *Informer) putCredential(credential *egressapi.EgressCredential) {
	parsed, err := resources.CredentialFromAPI(credential)
	if err != nil {
		slog.Warn("EgressCredential refused", "credential", credential.Name, "error", err)
		i.index.DeleteCredential(credential.Name)
		return
	}
	i.index.PutCredential(parsed)
}

func (i *Informer) putSecret(secret *corev1.Secret) {
	i.index.PutSecret(resources.SecretFromV1(secret))
}

// Run lists and watches until ctx is done; see reader.Watches.Run.
func (i *Informer) Run(ctx context.Context) { i.watches.Run(ctx) }
