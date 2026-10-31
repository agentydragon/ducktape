// Package policy decides what a sandbox workload may reach: the bindings that name it, the egress
// rules of the sets they grant, and the credential a request presents. It performs no I/O; the
// informer keeps the Index equal to the API server's.
package policy

import (
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

// Index is everything the decision reads, keyed by name. Its writer is the informer.
type Index struct {
	// Rules is the egress rules of the namespace's PolicySets and the bindings that grant them.
	Rules *reader.Index[resources.Rule]
	// Health covers the rules, credentials and Secrets alike.
	Health *watchhealth.Health

	mu          sync.RWMutex
	credentials map[string]resources.Credential
	secrets     map[string]resources.Secret
}

// NewIndex returns an empty Index that is unavailable until its informers sync, and while any of its
// kinds fails to list or watch.
func NewIndex() *Index {
	return &Index{
		Rules:       reader.NewIndex(resources.EgressRuleType, resources.ParseRule),
		Health:      watchhealth.New(),
		credentials: map[string]resources.Credential{},
		secrets:     map[string]resources.Secret{},
	}
}

func (i *Index) PutCredential(credential resources.Credential) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.credentials[credential.Name] = credential
}

func (i *Index) DeleteCredential(name string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	delete(i.credentials, name)
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
