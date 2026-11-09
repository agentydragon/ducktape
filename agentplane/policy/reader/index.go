package reader

import (
	"fmt"
	"log/slog"
	"slices"
	"sync"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"k8s.io/apimachinery/pkg/types"
)

// NumberedRule is one of a set's rules of the reader's type, numbered by its position among all of
// the set's rules, so a rule keeps its number whatever other readers' rules sit beside it.
type NumberedRule[R any] struct {
	Number int
	Rule   R
}

// PolicySet is a set as one reader sees it: only the rules of its type.
type PolicySet[R any] struct {
	Name       string
	Generation int64
	Rules      []NumberedRule[R]
}

// Index holds one reader's rules and the bindings that grant them, keyed by name. Its writer is
// the informer; readers take the state of one moment.
type Index[R any] struct {
	ruleType string
	parse    func(policyapi.Rule) (R, error)

	mu   sync.RWMutex
	sets map[string]PolicySet[R]
	// refused holds each set with a rule of this type that does not parse, which grants nothing.
	refused  map[string]Refusal
	bindings map[string]policyapi.PolicyBinding
	// expiries fire a change at each binding's expiresAt, when it stops granting with no event.
	expiries map[string]*time.Timer
	// identities holds the UID of each live Identity in the policy namespace, by name.
	identities map[string]types.UID
	changed    chan struct{}
	now        func() time.Time
}

// NewIndex returns an empty Index of the rules whose type is ruleType, each read by parse.
func NewIndex[R any](ruleType string, parse func(policyapi.Rule) (R, error)) *Index[R] {
	return &Index[R]{
		ruleType:   ruleType,
		parse:      parse,
		sets:       map[string]PolicySet[R]{},
		refused:    map[string]Refusal{},
		bindings:   map[string]policyapi.PolicyBinding{},
		expiries:   map[string]*time.Timer{},
		identities: map[string]types.UID{},
		changed:    make(chan struct{}),
		now:        time.Now,
	}
}

// Changed closes after the next index mutation. Obtain it before reading to avoid missing a change.
func (i *Index[R]) Changed() <-chan struct{} {
	i.mu.RLock()
	defer i.mu.RUnlock()
	return i.changed
}

// notifyChanged is called with mu held.
func (i *Index[R]) notifyChanged() {
	close(i.changed)
	i.changed = make(chan struct{})
}

// PutPolicySet adds or replaces a set. A set with a rule of this type that does not parse is
// refused whole: it grants nothing rather than part of what its author meant. Other readers' rule
// types are ignored.
func (i *Index[R]) PutPolicySet(set *policyapi.PolicySet) {
	parsed, err := i.parseSet(set)
	i.mu.Lock()
	defer i.mu.Unlock()
	defer i.notifyChanged()
	if err != nil {
		slog.Warn("PolicySet refused", "policySet", set.Name, "error", err)
		delete(i.sets, set.Name)
		i.refused[set.Name] = Refusal{Set: set.Name, Generation: set.Generation, Reason: err.Error()}
		return
	}
	delete(i.refused, set.Name)
	i.sets[set.Name] = parsed
}

func (i *Index[R]) DeletePolicySet(name string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	defer i.notifyChanged()
	delete(i.sets, name)
	delete(i.refused, name)
}

func (i *Index[R]) parseSet(set *policyapi.PolicySet) (PolicySet[R], error) {
	parsed := PolicySet[R]{Name: set.Name, Generation: set.Generation}
	for number, raw := range set.Spec.Rules {
		if raw.Type != i.ruleType {
			continue
		}
		rule, err := i.parse(raw)
		if err != nil {
			return PolicySet[R]{}, fmt.Errorf("rules[%d]: %w", number, err)
		}
		parsed.Rules = append(parsed.Rules, NumberedRule[R]{Number: number, Rule: rule})
	}
	return parsed, nil
}

// Refusal is a set with a rule of this type that does not parse, and why.
type Refusal struct {
	Set        string
	Generation int64
	Reason     string
}

// Resolution is how the sets a binding names stand for this reader, each list in the order named. A
// missing or refused set takes nothing from the others.
type Resolution[R any] struct {
	// Sets are the named sets that parse, those with no rules of this type included.
	Sets    []PolicySet[R]
	Missing []string
	Refused []Refusal
}

// Rules is how many rules of this type the sets that parse hold.
func (r Resolution[R]) Rules() int {
	rules := 0
	for _, set := range r.Sets {
		rules += len(set.Rules)
	}
	return rules
}

// Resolve reports how the named sets stand.
func (i *Index[R]) Resolve(names []string) Resolution[R] {
	i.mu.RLock()
	defer i.mu.RUnlock()
	return i.resolve(names)
}

func (i *Index[R]) resolve(names []string) Resolution[R] {
	var resolution Resolution[R]
	for _, name := range names {
		if set, ok := i.sets[name]; ok {
			resolution.Sets = append(resolution.Sets, set)
		} else if refusal, ok := i.refused[name]; ok {
			resolution.Refused = append(resolution.Refused, refusal)
		} else {
			resolution.Missing = append(resolution.Missing, name)
		}
	}
	return resolution
}

// PutBinding adds or replaces a binding. A binding grants while it exists and is live: the Policy
// Controller deletes it at expiry, and until then the index treats an expired one as absent.
func (i *Index[R]) PutBinding(binding *policyapi.PolicyBinding) {
	i.mu.Lock()
	defer i.mu.Unlock()
	defer i.notifyChanged()
	i.stopExpiry(binding.Name)
	i.bindings[binding.Name] = *binding.DeepCopy()
	if expiresAt := binding.Spec.ExpiresAt; expiresAt != nil {
		if remaining := expiresAt.Sub(i.now()); remaining > 0 {
			i.expiries[binding.Name] = time.AfterFunc(remaining, func() {
				i.mu.Lock()
				defer i.mu.Unlock()
				i.notifyChanged()
			})
		}
	}
}

func (i *Index[R]) DeleteBinding(name string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	defer i.notifyChanged()
	i.stopExpiry(name)
	delete(i.bindings, name)
}

// stopExpiry is called with mu held.
func (i *Index[R]) stopExpiry(name string) {
	if timer, ok := i.expiries[name]; ok {
		timer.Stop()
		delete(i.expiries, name)
	}
}

// PutIdentity records the UID of a live Identity. A binding names an Identity lookup only while the
// Identity of its name has the lookup's UID.
func (i *Index[R]) PutIdentity(identity *policyapi.Identity) {
	i.mu.Lock()
	defer i.mu.Unlock()
	defer i.notifyChanged()
	i.identities[identity.Name] = identity.UID
}

func (i *Index[R]) DeleteIdentity(name string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	defer i.notifyChanged()
	delete(i.identities, name)
}

// Subject is who a lookup asks about: a ServiceAccount or an Identity. A binding names exactly one
// kind of subject, so a lookup of one kind never finds a binding of the other.
type Subject interface{ subject() }

// ServiceAccount is a Kubernetes ServiceAccount, as the caller authenticated. It names the bindings
// whose subject is that ServiceAccount.
type ServiceAccount policyapi.ServiceAccountRef

// Identity is one incarnation of an Identity in the policy namespace: its name and UID. It names the
// bindings whose subject is that name, only while the live Identity of that name has that UID.
//
// UID fencing protects stale principals: a credential of a deleted incarnation gains nothing from a
// same-named successor. It does not end a binding. A binding's lifetime comes from its
// ownerReference to its Identity, which the app and Dispatcher must set, so garbage collection
// deletes an agent's bindings with the Identity.
//
// No enforcing service looks up an Identity yet. Its callers land next: the WorkloadCredential
// resolver (agentplane/docs/workload_authentication.md, "How a verifier resolves a peer") and
// SERVICES_IDENTITY_PRINCIPAL (agentplane/plans/task_dag.md).
type Identity struct {
	Name string
	UID  types.UID
}

func (ServiceAccount) subject() {}
func (Identity) subject()       {}

// Grant is one binding naming the caller, and the sets with rules of this type it grants.
type Grant[R any] struct {
	Binding string
	Sets    []PolicySet[R]
}

// Grants returns the bindings that give the caller rules of this type, in binding-name order. A
// binding names its sets in grant order; a missing or refused set takes nothing from the others,
// and a set with no rules of this type is another reader's and grants this one nothing.
func (i *Index[R]) Grants(caller Subject) []Grant[R] {
	i.mu.RLock()
	defer i.mu.RUnlock()
	var granted []Grant[R]
	for _, name := range i.naming(caller) {
		var sets []PolicySet[R]
		for _, setName := range i.bindings[name].Spec.PolicySets {
			if set, ok := i.sets[setName]; ok && len(set.Rules) > 0 {
				sets = append(sets, set)
			}
		}
		if len(sets) > 0 {
			granted = append(granted, Grant[R]{Binding: name, Sets: sets})
		}
	}
	return granted
}

// BindingResolution is a binding and how the sets it names stand.
type BindingResolution[R any] struct {
	Binding policyapi.PolicyBinding
	Resolution[R]
}

// Bindings returns every binding naming the subject, in binding-name order, with how the sets it
// names stand, including a binding that grants this reader nothing.
func (i *Index[R]) Bindings(subject Subject) []BindingResolution[R] {
	i.mu.RLock()
	defer i.mu.RUnlock()
	var found []BindingResolution[R]
	for _, name := range i.naming(subject) {
		binding := i.bindings[name]
		found = append(found, BindingResolution[R]{Binding: *binding.DeepCopy(), Resolution: i.resolve(binding.Spec.PolicySets)})
	}
	return found
}

// naming is the names of the live bindings naming the subject, sorted.
func (i *Index[R]) naming(subject Subject) []string {
	var names []string
	now := i.now()
	for name, binding := range i.bindings {
		if i.names(binding.Spec.Subject, subject) && policyapi.Live(binding.Spec.ExpiresAt, now) {
			names = append(names, name)
		}
	}
	slices.Sort(names)
	return names
}

// names reports whether a binding's subject is the lookup's.
func (i *Index[R]) names(bound policyapi.Subject, subject Subject) bool {
	switch subject := subject.(type) {
	case ServiceAccount:
		return bound.Identity == nil && bound.Namespace == subject.Namespace && bound.Name == subject.Name
	case Identity:
		live, ok := i.identities[subject.Name]
		return bound.Identity != nil && bound.Identity.Name == subject.Name && ok && live == subject.UID
	default:
		panic(fmt.Sprintf("unknown subject %T", subject))
	}
}
