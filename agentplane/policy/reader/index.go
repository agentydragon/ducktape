package reader

import (
	"fmt"
	"log/slog"
	"maps"
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
	Name  string
	Rules []NumberedRule[R]
}

// Index holds one reader's rules and the bindings that grant them, keyed by name. Its writer is
// the informer; readers take the state of one moment.
type Index[R any] struct {
	ruleType string
	parse    func(policyapi.Rule) (R, error)

	mu   sync.RWMutex
	sets map[string]PolicySet[R]
	// refused holds why each set with a rule of this type that does not parse grants nothing.
	refused  map[string]string
	bindings map[string]policyapi.PolicyBinding
}

// NewIndex returns an empty Index of the rules whose type is ruleType, each read by parse.
func NewIndex[R any](ruleType string, parse func(policyapi.Rule) (R, error)) *Index[R] {
	return &Index[R]{
		ruleType: ruleType,
		parse:    parse,
		sets:     map[string]PolicySet[R]{},
		refused:  map[string]string{},
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
		i.refused[set.Name] = err.Error()
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
	parsed := PolicySet[R]{Name: set.Name}
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
type Refusal struct{ Set, Reason string }

// Resolution is what the sets a binding names give this reader. A missing or refused set takes
// nothing from the others.
type Resolution struct {
	Missing []string
	Refused []Refusal
	// Rules is the rules of this type in the sets that did parse.
	Rules int
}

// Resolve reports how the named sets stand, in the order named.
func (i *Index[R]) Resolve(names []string) Resolution {
	i.mu.RLock()
	defer i.mu.RUnlock()
	var resolution Resolution
	for _, name := range names {
		if set, ok := i.sets[name]; ok {
			resolution.Rules += len(set.Rules)
		} else if reason, ok := i.refused[name]; ok {
			resolution.Refused = append(resolution.Refused, Refusal{Set: name, Reason: reason})
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
	for _, name := range slices.Sorted(maps.Keys(i.bindings)) {
		binding := i.bindings[name]
		if binding.Spec.Subject != caller {
			continue
		}
		var sets []PolicySet[R]
		for _, setName := range binding.Spec.PolicySets {
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
