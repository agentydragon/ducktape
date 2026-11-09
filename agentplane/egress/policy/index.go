// Package policy decides what a sandbox workload may reach: the bindings that name it, the egress
// rules of the sets they grant, and the credential a request presents. Its only I/O is reading the
// proxy's own API server token for a request a kubernetesServiceAccount credential decides; the
// informer keeps the Index equal to the API server's.
package policy

import (
	"slices"
	"strings"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
)

// The kinds the proxy watches beside PolicySets and PolicyBindings.
const (
	Credentials watchhealth.Kind = resources.CredentialsPlural
	Secrets     watchhealth.Kind = "secrets"
)

// APIServer is the API server a kubernetesServiceAccount credential acts on: where requests to it go,
// and the proxy's own bearer for it.
type APIServer struct {
	Host string
	Port int
	// Token reads the proxy's bearer. The proxy reads it once per admitted request (Request's
	// APIServerToken), so a rotated token file is used from the next request on.
	Token func() (string, error)
}

// Index is everything the decision reads, keyed by name. Its writer is the informer.
type Index struct {
	// Rules is the egress rules of the namespace's PolicySets and the bindings that grant them.
	Rules *reader.Index[resources.Rule]
	// Health covers the rules, credentials and Secrets alike.
	Health *watchhealth.Health
	// LocalAPIServer is the proxy's own API server, set before serving; nil leaves every
	// kubernetesServiceAccount credential unavailable.
	LocalAPIServer *APIServer

	mu                sync.RWMutex
	credentials       map[string]resources.Credential
	credentialChanged chan struct{}
	secrets           map[string]resources.Secret
}

// NewIndex returns an empty Index that is unavailable until its informers sync, and while any of its
// kinds fails to list or watch.
func NewIndex() *Index {
	return &Index{
		Rules:             reader.NewIndex(resources.EgressRuleType, resources.ParseRule),
		Health:            watchhealth.New(),
		credentials:       map[string]resources.Credential{},
		credentialChanged: make(chan struct{}),
		secrets:           map[string]resources.Secret{},
	}
}

func (i *Index) PutCredential(credential resources.Credential) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.credentials[credential.Name] = credential
	close(i.credentialChanged)
	i.credentialChanged = make(chan struct{})
}

func (i *Index) DeleteCredential(name string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	delete(i.credentials, name)
	close(i.credentialChanged)
	i.credentialChanged = make(chan struct{})
}

func (i *Index) PutSecret(secret resources.Secret) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.secrets[secret.Name] = secret
}

func (i *Index) DeleteSecret(name string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	delete(i.secrets, name)
}

// CredentialsChanged closes after the next credential mutation. Obtain it before Credentials.
func (i *Index) CredentialsChanged() <-chan struct{} {
	i.mu.RLock()
	defer i.mu.RUnlock()
	return i.credentialChanged
}

// Credentials returns the current credential inventory in stable name order.
func (i *Index) Credentials() []resources.Credential {
	i.mu.RLock()
	defer i.mu.RUnlock()
	out := make([]resources.Credential, 0, len(i.credentials))
	for _, credential := range i.credentials {
		out = append(out, credential)
	}
	slices.SortFunc(out, func(a, b resources.Credential) int { return strings.Compare(a.Name, b.Name) })
	return out
}
