package reader

import (
	"fmt"
	"log/slog"
	"slices"
	"sync"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
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
}

// NewIndex returns an empty Index of the rules whose type is ruleType, each read by parse.
func NewIndex[R any](ruleType string, parse func(policyapi.Rule) (R, error)) *Index[R] {
	return &Index[R]{
		ruleType: ruleType,
		parse:    parse,
		sets:     map[string]PolicySet[R]{},
		refused:  map[string]Refusal{},
		bindings: map[string]policyapi.PolicyBinding{},
	}
}

// PutPolicySet adds or replaces a set. A set with a rule of this type that does not parse is
// refused whole: it grants nothing rather than part of what its author meant. Other readers' rule
// types are ignored.
func (i *Index[R]) PutPolicySet(set *policyapi.PolicySet) {
	parsed, err := i.parseSet(set)
	i.mu.Lock()
	defer i.mu.Unlock()
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

// PutBinding adds or replaces a binding. A binding that exists grants: the Policy Controller
// deletes it at expiry.
func (i *Index[R]) PutBinding(binding *policyapi.PolicyBinding) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.bindings[binding.Name] = *binding.DeepCopy()
}

func (i *Index[R]) DeleteBinding(name string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	delete(i.bindings, name)
}

// Grant is one binding naming the caller, and the sets with rules of this type it grants.
type Grant[R any] struct {
	Binding string
	Sets    []PolicySet[R]
}

// Grants returns the bindings that give the caller rules of this type, in binding-name order. A
// binding names its sets in grant order; a missing or refused set takes nothing from the others,
// and a set with no rules of this type is another reader's and grants this one nothing.
func (i *Index[R]) Grants(caller policyapi.ServiceAccountRef) []Grant[R] {
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
func (i *Index[R]) Bindings(subject policyapi.ServiceAccountRef) []BindingResolution[R] {
	i.mu.RLock()
	defer i.mu.RUnlock()
	var found []BindingResolution[R]
	for _, name := range i.naming(subject) {
		binding := i.bindings[name]
		found = append(found, BindingResolution[R]{Binding: *binding.DeepCopy(), Resolution: i.resolve(binding.Spec.PolicySets)})
	}
	return found
}

// naming is the names of the bindings naming the subject, sorted.
func (i *Index[R]) naming(subject policyapi.ServiceAccountRef) []string {
	var names []string
	for name, binding := range i.bindings {
		if binding.Spec.Subject == subject {
			names = append(names, name)
		}
	}
	slices.Sort(names)
	return names
}
