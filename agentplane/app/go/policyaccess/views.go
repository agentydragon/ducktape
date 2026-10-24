package policyaccess

import (
	"encoding/json"
	"fmt"
	"sort"
	"unicode/utf8"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const maxRefusalReasonCharacters = 32_768

const egressRuleType = "egress"

// CredentialView describes how a subject presents a credential without exposing its value.
type CredentialView struct {
	Name        string             `json:"name"`
	Description string             `json:"description"`
	Placeholder string             `json:"placeholder"`
	Secret      *string            `json:"secret"`
	Key         *string            `json:"key"`
	Targets     []egressapi.Target `json:"targets" nullable:"false"`
}

// RuleView is a typed discriminated union. Egress fields are populated when Kind is "egress";
// Type and Fields are populated when Kind is "other". Its JSON representation contains only the
// fields for the selected variant.
type RuleView struct {
	Kind              string          `json:"kind"`
	Number            int             `json:"number"`
	Type              string          `json:"type,omitempty"`
	Fields            map[string]any  `json:"fields,omitempty"`
	Hosts             []string        `json:"hosts,omitempty"`
	Methods           []string        `json:"methods,omitempty"`
	Paths             []string        `json:"paths,omitempty"`
	Credential        *CredentialView `json:"credential,omitempty"`
	MissingCredential *string         `json:"missing_credential,omitempty"`
}

func (rule RuleView) MarshalJSON() ([]byte, error) {
	switch rule.Kind {
	case "egress":
		return json.Marshal(struct {
			Kind              string          `json:"kind"`
			Number            int             `json:"number"`
			Hosts             []string        `json:"hosts"`
			Methods           []string        `json:"methods"`
			Paths             []string        `json:"paths"`
			Credential        *CredentialView `json:"credential"`
			MissingCredential *string         `json:"missing_credential"`
		}{
			Kind: rule.Kind, Number: rule.Number, Hosts: rule.Hosts, Methods: rule.Methods,
			Paths: rule.Paths, Credential: rule.Credential, MissingCredential: rule.MissingCredential,
		})
	case "other":
		return json.Marshal(struct {
			Kind   string         `json:"kind"`
			Number int            `json:"number"`
			Type   string         `json:"type"`
			Fields map[string]any `json:"fields"`
		}{Kind: rule.Kind, Number: rule.Number, Type: rule.Type, Fields: rule.Fields})
	default:
		return nil, fmt.Errorf("unknown Policy rule view kind %q", rule.Kind)
	}
}

// PolicySetView carries every rule in the set in source order. A set with an invalid egress rule
// is refused as a whole for egress and has no projected rules.
type PolicySetView struct {
	Name       string     `json:"name"`
	Generation int64      `json:"generation"`
	Rules      []RuleView `json:"rules" nullable:"false"`
	Refused    *string    `json:"refused"`
}

// ActionPolicySetView is the Action Service inventory projection returned to the browser.
type ActionPolicySetView struct {
	Name       string  `json:"name"`
	Generation int64   `json:"generation"`
	Refused    *string `json:"refused"`
}

// BindingProvenance identifies the authority that owns a binding's source of truth.
type BindingProvenance string

const (
	BindingFromGit      BindingProvenance = "git"
	BindingFromSandbox  BindingProvenance = "sandbox"
	BindingFromOperator BindingProvenance = "operator"
)

// ConditionView is one policy reader's verdict, with its observed generation when present.
type ConditionView struct {
	Type               string                 `json:"type"`
	Status             metav1.ConditionStatus `json:"status"`
	Reason             string                 `json:"reason"`
	Message            string                 `json:"message"`
	ObservedGeneration *int64                 `json:"observed_generation"`
}

// BindingView projects one binding and all policy sets it names, including missing names and
// reader conditions. Credentials contribute only their public metadata and Secret reference.
type BindingView struct {
	Name              string                        `json:"name"`
	Provenance        BindingProvenance             `json:"provenance"`
	Generation        int64                         `json:"generation"`
	Subjects          []policyapi.ServiceAccountRef `json:"subjects" nullable:"false"`
	ExpiresAt         *metav1.Time                  `json:"expires_at"`
	Conditions        []ConditionView               `json:"conditions" nullable:"false"`
	PolicySets        []PolicySetView               `json:"policy_sets" nullable:"false"`
	MissingPolicySets []string                      `json:"missing_policy_sets" nullable:"false"`
}

func policySetViews(
	sets []policyapi.PolicySet,
	credentials map[string]CredentialView,
) map[string]PolicySetView {
	views := make(map[string]PolicySetView, len(sets))
	for index := range sets {
		set := &sets[index]
		view := PolicySetView{
			Name: set.Name, Generation: set.Generation,
			Rules: make([]RuleView, 0, len(set.Spec.Rules)),
		}
		for number, rule := range set.Spec.Rules {
			if rule.Type != egressRuleType {
				fields, err := otherRuleFields(rule)
				if err != nil {
					refused := truncateReason(fmt.Sprintf("rules[%d]: %v", number, err))
					view.Refused = &refused
					view.Rules = []RuleView{}
					break
				}
				view.Rules = append(view.Rules, RuleView{
					Kind: "other", Number: number, Type: rule.Type, Fields: fields,
				})
				continue
			}
			if err := validateEgressRule(rule); err != nil {
				refused := truncateReason(fmt.Sprintf("rules[%d]: %v", number, err))
				view.Refused = &refused
				view.Rules = []RuleView{}
				break
			}
			egressView := RuleView{
				Kind: "egress", Number: number, Hosts: copyStrings(rule.Hosts),
				Methods: copyStrings(rule.Methods), Paths: copyStrings(rule.Paths),
			}
			if rule.CredentialRef != nil {
				name := rule.CredentialRef.Name
				if credential, ok := credentials[name]; ok {
					egressView.Credential = &credential
				} else {
					egressView.MissingCredential = &name
				}
			}
			view.Rules = append(view.Rules, egressView)
		}
		views[set.Name] = view
	}
	return views
}

func credentialViews(credentials []egressapi.EgressCredential) (map[string]CredentialView, error) {
	views := make(map[string]CredentialView, len(credentials))
	for index := range credentials {
		resource := &credentials[index]
		view, err := credentialView(resource)
		if err != nil {
			return nil, fmt.Errorf("read EgressCredential %q: %w", resource.Name, err)
		}
		views[view.Name] = view
	}
	return views, nil
}

func otherRuleFields(rule policyapi.Rule) (map[string]any, error) {
	encoded, err := json.Marshal(rule)
	if err != nil {
		return nil, err
	}
	var fields map[string]any
	if err := json.Unmarshal(encoded, &fields); err != nil {
		return nil, err
	}
	delete(fields, "type")
	return fields, nil
}

func bindingViews(
	bindings []policyapi.PolicyBinding,
	sets map[string]PolicySetView,
	subject policyapi.ServiceAccountRef,
) []BindingView {
	views := make([]BindingView, 0)
	for index := range bindings {
		binding := &bindings[index]
		if binding.Spec.Subject != subject {
			continue
		}
		view := BindingView{
			Name: binding.Name, Provenance: bindingProvenance(binding), Generation: binding.Generation,
			Subjects: []policyapi.ServiceAccountRef{binding.Spec.Subject}, ExpiresAt: binding.Spec.ExpiresAt,
			Conditions: make([]ConditionView, 0, len(binding.Status.Conditions)),
			PolicySets: make([]PolicySetView, 0, len(binding.Spec.PolicySets)), MissingPolicySets: []string{},
		}
		for _, condition := range binding.Status.Conditions {
			conditionView := ConditionView{
				Type: condition.Type, Status: condition.Status, Reason: condition.Reason, Message: condition.Message,
			}
			if condition.ObservedGeneration != 0 {
				generation := condition.ObservedGeneration
				conditionView.ObservedGeneration = &generation
			}
			view.Conditions = append(view.Conditions, conditionView)
		}
		sortConditions(view.Conditions)
		for _, name := range binding.Spec.PolicySets {
			if policySet, ok := sets[name]; ok {
				view.PolicySets = append(view.PolicySets, policySet)
			} else {
				view.MissingPolicySets = append(view.MissingPolicySets, name)
			}
		}
		views = append(views, view)
	}
	sortBindings(views)
	return views
}

func credentialProjection(
	sets []policyapi.PolicySet,
	credentials []egressapi.EgressCredential,
) (map[string]PolicySetView, error) {
	byName, err := credentialViews(credentials)
	if err != nil {
		return nil, err
	}
	return policySetViews(sets, byName), nil
}

func truncateReason(reason string) string {
	if utf8.RuneCountInString(reason) <= maxRefusalReasonCharacters {
		return reason
	}
	runes := []rune(reason)
	return string(runes[:maxRefusalReasonCharacters])
}

func copyStrings(values []string) []string {
	if values == nil {
		return nil
	}
	return append([]string(nil), values...)
}

func stringPointer(value string) *string { return &value }

func credentialView(credential *egressapi.EgressCredential) (CredentialView, error) {
	name := credential.Name
	if name == "" {
		return CredentialView{}, fmt.Errorf("EgressCredential: metadata.name is required")
	}
	if credential.Spec.Description == "" {
		return CredentialView{}, fmt.Errorf("EgressCredential %s: spec.description is required", name)
	}
	if len(credential.Spec.Targets) == 0 {
		return CredentialView{}, fmt.Errorf("EgressCredential %s: spec.targets needs at least one target", name)
	}
	sourceCount := 0
	if credential.Spec.Source.SecretRef != nil {
		sourceCount++
	}
	if credential.Spec.Source.AuthenticatedWorkloadToken != nil {
		sourceCount++
	}
	if projected := credential.Spec.Source.ProjectedWorkloadToken; projected != nil {
		sourceCount++
		if projected.Audience == "" {
			return CredentialView{}, fmt.Errorf("EgressCredential %s: spec.source: projectedWorkloadToken.audience is required", name)
		}
	}
	if sourceCount != 1 {
		return CredentialView{}, fmt.Errorf("EgressCredential %s: spec.source: source must set exactly one of secretRef, authenticatedWorkloadToken or projectedWorkloadToken", name)
	}
	view := CredentialView{
		Name: name, Description: credential.Spec.Description,
		Placeholder: "agentplane-credential-" + name,
		Targets:     make([]egressapi.Target, 0, len(credential.Spec.Targets)),
	}
	if secret := credential.Spec.Source.SecretRef; secret != nil {
		view.Secret = stringPointer(secret.Name)
		view.Key = stringPointer(secret.Key)
	}
	for index, target := range credential.Spec.Targets {
		if err := validateTarget(target); err != nil {
			return CredentialView{}, fmt.Errorf("EgressCredential %s: spec.targets[%d]: %w", name, index, err)
		}
		view.Targets = append(view.Targets, target)
	}
	return view, nil
}

func validateTarget(target egressapi.Target) error {
	switch target.Method {
	case egressapi.WholeValue, egressapi.BasicUsername, egressapi.BasicPassword, egressapi.BasicWhole:
		if target.Header == "" {
			return fmt.Errorf("header is required")
		}
		if target.Field != "" || target.Scheme != "" {
			return fmt.Errorf("%s takes no field or scheme", target.Method)
		}
	case egressapi.SchemeToken:
		if target.Header == "" {
			return fmt.Errorf("header is required")
		}
		if target.Scheme == "" {
			return fmt.Errorf("schemeToken needs a scheme")
		}
		if target.Field != "" {
			return fmt.Errorf("schemeToken takes no field")
		}
	case egressapi.JSONField:
		if target.Field == "" || len(target.Field) > 256 {
			return fmt.Errorf("jsonField.field must be 1 to 256 characters")
		}
		if target.Header != "" || target.Scheme != "" {
			return fmt.Errorf("jsonField takes only a field")
		}
	default:
		return fmt.Errorf("unknown target method %q", target.Method)
	}
	return nil
}

func validateEgressRule(rule policyapi.Rule) error {
	if len(rule.Hosts) == 0 || len(rule.Hosts) > 256 {
		return fmt.Errorf("hosts needs 1 to 256 entries")
	}
	blanket := false
	for _, host := range rule.Hosts {
		if len(host) > 256 {
			return fmt.Errorf("a host is longer than 256 characters")
		}
		blanket = blanket || host == "*"
	}
	if blanket && (rule.ClusterInternal || rule.CredentialRef != nil) {
		return fmt.Errorf("a blanket host rule cannot set clusterInternal or credentialRef")
	}
	return nil
}

func sortConditions(conditions []ConditionView) {
	sort.Slice(conditions, func(i, j int) bool { return conditions[i].Type < conditions[j].Type })
}

func sortBindings(bindings []BindingView) {
	sort.Slice(bindings, func(i, j int) bool { return bindings[i].Name < bindings[j].Name })
}

func bindingProvenance(binding *policyapi.PolicyBinding) BindingProvenance {
	if _, ok := binding.Labels[fluxKustomizationLabel]; ok {
		return BindingFromGit
	}
	for _, owner := range binding.OwnerReferences {
		if owner.Controller != nil && *owner.Controller && owner.Kind == "Sandbox" {
			return BindingFromSandbox
		}
	}
	return BindingFromOperator
}

const fluxKustomizationLabel = "kustomize.toolkit.fluxcd.io/name"

var _ metav1.ConditionStatus = metav1.ConditionTrue
