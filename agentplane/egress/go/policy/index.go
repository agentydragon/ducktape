// Package policy decides what a sandbox workload may reach: the bindings that name it, the egress
// rules of the sets they grant, and the credential a request presents. It performs no I/O; the
// informer keeps the Index equal to the API server's.
package policy

import (
	"fmt"
	"log/slog"
	"maps"
	"slices"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/go/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
)

// Kind is a resource kind the Index is built from, by plural.
type Kind string

const (
	Credentials Kind = resources.CredentialsPlural
	PolicySets  Kind = "policysets"
	Bindings    Kind = "policybindings"
	Secrets     Kind = "secrets"
)

var watchedKinds = []Kind{Credentials, PolicySets, Bindings, Secrets}

// NumberedRule is one of a set's egress rules, numbered by its position among all of the set's
// rules, so a rule keeps its number whatever other readers' rules sit beside it.
type NumberedRule struct {
	Number int
	Rule   resources.Rule
}

// PolicySet is a set as this proxy sees it: only its egress rules.
type PolicySet struct {
	Name  string
	Rules []NumberedRule
}

// Index is everything the decision reads, keyed by name. Its writer is the informer; readers
// take the state of one moment.
type Index struct {
	staleAfter time.Duration

	mu          sync.RWMutex
	sets        map[string]PolicySet
	bindings    map[string]policyapi.PolicyBinding
	credentials map[string]resources.Credential
	secrets     map[string]resources.Secret
	// cycles holds when each watched kind last completed a full list-and-watch cycle. Under health
	// every entry advances at the watch period; one that stops is the failure this exists to
	// expose: a wedged list or a watch that never returns leaves the index frozen while every
	// answer it gives stays plausible and `synced` stays true.
	cycles   map[Kind]time.Time
	synced   bool
	draining bool
}

// NewIndex returns an empty Index that stops being available once a kind's last completed cycle is
// older than staleAfter.
func NewIndex(staleAfter time.Duration) *Index {
	return &Index{
		staleAfter:  staleAfter,
		sets:        map[string]PolicySet{},
		bindings:    map[string]policyapi.PolicyBinding{},
		credentials: map[string]resources.Credential{},
		secrets:     map[string]resources.Secret{},
		cycles:      map[Kind]time.Time{},
	}
}

// Available reports whether the index may be acted on: it has finished its first sync, every
// watched kind completed a cycle recently, and the proxy is not draining.
func (i *Index) Available(now time.Time) bool {
	i.mu.RLock()
	defer i.mu.RUnlock()
	if i.draining || !i.synced {
		return false
	}
	for _, kind := range watchedKinds {
		// A negative age is a clock that moved backwards, which is no more trustworthy than a late one.
		completed, ok := i.cycles[kind]
		if age := now.Sub(completed); !ok || age < 0 || age > i.staleAfter {
			return false
		}
	}
	return true
}

// MarkSynced records that the first list of every kind has been applied. It never falls back;
// recent cycles are what keep the index available after.
func (i *Index) MarkSynced() {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.synced = true
}

// RecordCycle records that kind completed a list-and-watch cycle at the given instant. The informer
// seeds every kind with its start time before listing, so a kind that never finishes one goes
// stale like any other.
func (i *Index) RecordCycle(kind Kind, at time.Time) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.cycles[kind] = at
}

// MarkDraining makes the index unavailable for good: the proxy admits nothing new.
func (i *Index) MarkDraining() {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.draining = true
}

// PutPolicySet adds or replaces a set. A set with an egress rule that does not parse is refused
// whole: it grants this proxy nothing rather than part of what its author meant. Other readers'
// rule types are ignored.
func (i *Index) PutPolicySet(set *policyapi.PolicySet) {
	parsed, err := parseSet(set)
	i.mu.Lock()
	defer i.mu.Unlock()
	if err != nil {
		slog.Warn("PolicySet refused", "policySet", set.Name, "error", err)
		delete(i.sets, set.Name)
		return
	}
	i.sets[set.Name] = parsed
}

func (i *Index) DeletePolicySet(name string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	delete(i.sets, name)
}

func parseSet(set *policyapi.PolicySet) (PolicySet, error) {
	parsed := PolicySet{Name: set.Name}
	for number, raw := range set.Spec.Rules {
		if raw.Type != resources.EgressRuleType {
			continue
		}
		rule, err := resources.ParseRule(raw)
		if err != nil {
			return PolicySet{}, fmt.Errorf("rules[%d]: %w", number, err)
		}
		parsed.Rules = append(parsed.Rules, NumberedRule{Number: number, Rule: rule})
	}
	return parsed, nil
}

// PutBinding adds or replaces a binding. A binding that exists grants: the Policy Controller
// deletes it at expiry.
func (i *Index) PutBinding(binding *policyapi.PolicyBinding) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.bindings[binding.Name] = *binding.DeepCopy()
}

func (i *Index) DeleteBinding(name string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	delete(i.bindings, name)
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

// grant is one binding naming the caller, and the sets with egress rules it grants.
type grant struct {
	binding string
	sets    []PolicySet
}

// grants returns the bindings that give the caller egress rules, in binding-name order. A binding
// names its sets in grant order; a missing or refused set takes nothing from the others, and a
// set with no egress rules is another reader's and grants this proxy nothing. The caller holds
// i.mu.
func (i *Index) grants(caller policyapi.ServiceAccountRef) []grant {
	var granted []grant
	for _, name := range slices.Sorted(maps.Keys(i.bindings)) {
		binding := i.bindings[name]
		if binding.Spec.Subject != caller {
			continue
		}
		var sets []PolicySet
		for _, setName := range binding.Spec.PolicySets {
			if set, ok := i.sets[setName]; ok && len(set.Rules) > 0 {
				sets = append(sets, set)
			}
		}
		if len(sets) > 0 {
			granted = append(granted, grant{binding: name, sets: sets})
		}
	}
	return granted
}
