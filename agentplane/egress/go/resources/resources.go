// Package resources reads the objects the egress proxy gets from the API server into the types its
// decision runs on, once, at the boundary: an EgressCredential, the egress rules of a PolicySet,
// and a Secret.
package resources

import (
	"errors"
	"fmt"
	"regexp"
	"strings"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
	corev1 "k8s.io/api/core/v1"
)

const (
	// CredentialsPlural is the plural of the EgressCredential kind.
	CredentialsPlural = "egresscredentials"
	// EgressRuleType is the PolicySet rule type this proxy enforces.
	EgressRuleType = "egress"
	// PlaceholderPrefix starts every placeholder; see Placeholder.
	PlaceholderPrefix = "agentplane-credential-"

	maxHostsPerRule = 256
	maxHostLength   = 256
	maxFieldLength  = 256
)

// Placeholder is what a sandbox sends where the credential goes. It is derived and never
// authored, so one placeholder means one credential without anything having to check that two
// spellings agree; the separator is `-` and not `:` because a `:` cannot be a basicUsername
// component, which ends at the first one.
func Placeholder(credentialName string) string { return PlaceholderPrefix + credentialName }

// Target is one exact location a credential may be presented in: a closed set of variants,
// dispatched on with a type switch. Each names a total parse of that value into the credential
// component and the text around it; adding a presentation means adding a variant, never loosening one.
type Target interface {
	isTarget()
	// ToAPI is the target as an EgressCredential declares it, which is also how a sandbox is told it.
	ToAPI() egressapi.Target
}

// WholeValue is `<credential>`: the header value is the credential and nothing else.
type WholeValue struct{ Header string }

// SchemeToken is `<scheme> <credential>`; the scheme is compared case-insensitively.
type SchemeToken struct{ Header, Scheme string }

// BasicUsername is `Basic base64(<credential>:<password>)`.
type BasicUsername struct{ Header string }

// BasicPassword is `Basic base64(<username>:<credential>)`.
type BasicPassword struct{ Header string }

// BasicWhole is `Basic base64(<credential>)`: a payload that is the credential, colon or no colon.
type BasicWhole struct{ Header string }

// JSONField is an exact string value in one top-level JSON object field; no paths or templates.
type JSONField struct{ Field string }

func (WholeValue) isTarget()    {}
func (SchemeToken) isTarget()   {}
func (BasicUsername) isTarget() {}
func (BasicPassword) isTarget() {}
func (BasicWhole) isTarget()    {}
func (JSONField) isTarget()     {}

func (t WholeValue) ToAPI() egressapi.Target {
	return egressapi.Target{Method: egressapi.WholeValue, Header: t.Header}
}

func (t SchemeToken) ToAPI() egressapi.Target {
	return egressapi.Target{Method: egressapi.SchemeToken, Header: t.Header, Scheme: t.Scheme}
}

func (t BasicUsername) ToAPI() egressapi.Target {
	return egressapi.Target{Method: egressapi.BasicUsername, Header: t.Header}
}

func (t BasicPassword) ToAPI() egressapi.Target {
	return egressapi.Target{Method: egressapi.BasicPassword, Header: t.Header}
}

func (t BasicWhole) ToAPI() egressapi.Target {
	return egressapi.Target{Method: egressapi.BasicWhole, Header: t.Header}
}

func (t JSONField) ToAPI() egressapi.Target {
	return egressapi.Target{Method: egressapi.JSONField, Field: t.Field}
}

// Source is where a credential's value comes from: a closed set of variants.
type Source interface{ isSource() }

// SecretKey reads a key of a Secret in the credentials namespace.
type SecretKey struct{ Name, Key string }

// AuthenticatedWorkloadToken is the bearer retained from this request's successful workload authentication.
type AuthenticatedWorkloadToken struct{}

// ProjectedWorkloadToken is a token for another audience, projected into the sidecar and presented
// on the hop. The hop bearer carries the proxy's own audience, so a destination validating its own
// refuses it; this source is the same Pod's identity minted for that destination instead.
type ProjectedWorkloadToken struct{ Audience string }

func (SecretKey) isSource()                  {}
func (AuthenticatedWorkloadToken) isSource() {}
func (ProjectedWorkloadToken) isSource()     {}

// Credential is a credential a sandbox may present without holding, named so that its
// placeholder is unique.
type Credential struct {
	Name        string
	Description string
	Source      Source
	Targets     []Target
}

func (c Credential) Placeholder() string { return Placeholder(c.Name) }

// HasJSONFieldTarget reports whether a body, not only a header, can carry this credential.
func (c Credential) HasJSONFieldTarget() bool {
	for _, target := range c.Targets {
		if _, ok := target.(JSONField); ok {
			return true
		}
	}
	return false
}

// CredentialFromAPI reads one EgressCredential into the closed variants the proxy dispatches on.
// The CRD schema rejects what is malformed before it is stored; this is the proxy's own check,
// because a credential read wrongly is a placeholder forwarded raw or a secret sent where it
// should not go.
func CredentialFromAPI(credential *egressapi.EgressCredential) (Credential, error) {
	name := credential.Name
	if name == "" {
		return Credential{}, errors.New("EgressCredential: metadata.name is required")
	}
	if credential.Spec.Description == "" {
		return Credential{}, fmt.Errorf("EgressCredential %s: spec.description is required", name)
	}
	if len(credential.Spec.Targets) == 0 {
		return Credential{}, fmt.Errorf("EgressCredential %s: spec.targets needs at least one target", name)
	}
	source, err := sourceFromAPI(credential.Spec.Source)
	if err != nil {
		return Credential{}, fmt.Errorf("EgressCredential %s: spec.source: %w", name, err)
	}
	parsed := Credential{Name: name, Description: credential.Spec.Description, Source: source}
	for i, target := range credential.Spec.Targets {
		targeted, err := targetFromAPI(target)
		if err != nil {
			return Credential{}, fmt.Errorf("EgressCredential %s: spec.targets[%d]: %w", name, i, err)
		}
		parsed.Targets = append(parsed.Targets, targeted)
	}
	return parsed, nil
}

func sourceFromAPI(source egressapi.CredentialSource) (Source, error) {
	var parsed []Source
	if source.SecretRef != nil {
		parsed = append(parsed, SecretKey{Name: source.SecretRef.Name, Key: source.SecretRef.Key})
	}
	if source.AuthenticatedWorkloadToken != nil {
		parsed = append(parsed, AuthenticatedWorkloadToken{})
	}
	if source.ProjectedWorkloadToken != nil {
		if source.ProjectedWorkloadToken.Audience == "" {
			return nil, errors.New("projectedWorkloadToken.audience is required")
		}
		parsed = append(parsed, ProjectedWorkloadToken{Audience: source.ProjectedWorkloadToken.Audience})
	}
	if len(parsed) != 1 {
		return nil, errors.New("source must set exactly one of secretRef, authenticatedWorkloadToken or projectedWorkloadToken")
	}
	return parsed[0], nil
}

func targetFromAPI(target egressapi.Target) (Target, error) {
	if target.Method == egressapi.JSONField {
		if target.Header != "" || target.Scheme != "" {
			return nil, errors.New("jsonField takes only a field")
		}
		if target.Field == "" || len(target.Field) > maxFieldLength {
			return nil, fmt.Errorf("jsonField.field must be 1 to %d characters", maxFieldLength)
		}
		return JSONField{Field: target.Field}, nil
	}
	if target.Header == "" {
		return nil, errors.New("header is required")
	}
	if target.Field != "" {
		return nil, fmt.Errorf("%s takes no field", target.Method)
	}
	if target.Method != egressapi.SchemeToken && target.Scheme != "" {
		return nil, fmt.Errorf("%s takes no scheme", target.Method)
	}
	switch target.Method {
	case egressapi.WholeValue:
		return WholeValue{Header: target.Header}, nil
	case egressapi.SchemeToken:
		if target.Scheme == "" {
			return nil, errors.New("schemeToken needs a scheme")
		}
		return SchemeToken{Header: target.Header, Scheme: target.Scheme}, nil
	case egressapi.BasicUsername:
		return BasicUsername{Header: target.Header}, nil
	case egressapi.BasicPassword:
		return BasicPassword{Header: target.Header}, nil
	case egressapi.BasicWhole:
		return BasicWhole{Header: target.Header}, nil
	}
	return nil, fmt.Errorf("unknown target method %q", target.Method)
}

// Rule is an egress rule of a PolicySet: the hosts, methods and paths it admits and the credential
// it substitutes.
type Rule struct {
	// Hosts are exact hosts, `*.` suffix wildcards (`*.github.com`), or `*` for any public destination.
	Hosts []string
	// Methods are the HTTP methods admitted. Nil admits any; a present empty list admits none.
	Methods []string
	// Paths are globs: `*` within one segment, `**` across segments. Nil admits any; a present
	// empty list admits none.
	Paths []PathGlob
	// CredentialRef names the EgressCredential in the policy's namespace this rule substitutes.
	CredentialRef *string
	// ClusterInternal declares the hosts are inside the cluster and meant to be, so the proxy's
	// refusal of private addresses does not apply to them.
	ClusterInternal bool
}

// PathGlob is a path pattern compiled once.
type PathGlob struct {
	Pattern string
	regex   *regexp.Regexp
}

// NewPathGlob compiles a glob over the path without its query: `*` stays within a segment,
// `**` crosses segments.
func NewPathGlob(pattern string) PathGlob {
	var regex strings.Builder
	regex.WriteString(`\A(?:`)
	for rest := pattern; rest != ""; {
		switch {
		case strings.HasPrefix(rest, "**"):
			regex.WriteString(".*")
			rest = rest[2:]
		case rest[0] == '*':
			regex.WriteString("[^/]*")
			rest = rest[1:]
		default:
			next := strings.IndexByte(rest, '*')
			if next < 0 {
				next = len(rest)
			}
			regex.WriteString(regexp.QuoteMeta(rest[:next]))
			rest = rest[next:]
		}
	}
	regex.WriteString(`)\z`)
	return PathGlob{Pattern: pattern, regex: regexp.MustCompile(regex.String())}
}

// Matches reports whether the glob matches the path, ignoring any query.
func (g PathGlob) Matches(path string) bool {
	path, _, _ = strings.Cut(path, "?")
	return g.regex.MatchString(path)
}

// ParseRule validates one egress rule of a PolicySet.
func ParseRule(rule policyapi.Rule) (Rule, error) {
	if len(rule.Hosts) == 0 || len(rule.Hosts) > maxHostsPerRule {
		return Rule{}, fmt.Errorf("hosts needs 1 to %d entries", maxHostsPerRule)
	}
	blanket := false
	for _, host := range rule.Hosts {
		if len(host) > maxHostLength {
			return Rule{}, fmt.Errorf("a host is longer than %d characters", maxHostLength)
		}
		blanket = blanket || host == "*"
	}
	if blanket && (rule.ClusterInternal || rule.CredentialRef != nil) {
		return Rule{}, errors.New("a blanket host rule cannot set clusterInternal or credentialRef")
	}
	parsed := Rule{Hosts: rule.Hosts, Methods: rule.Methods, ClusterInternal: rule.ClusterInternal}
	if rule.Paths != nil {
		parsed.Paths = make([]PathGlob, len(rule.Paths))
		for i, pattern := range rule.Paths {
			parsed.Paths[i] = NewPathGlob(pattern)
		}
	}
	if rule.CredentialRef != nil {
		parsed.CredentialRef = &rule.CredentialRef.Name
	}
	return parsed, nil
}

// Secret is one Secret's data, never logged: it holds credential values.
type Secret struct {
	Name string
	data map[string]string
}

// NewSecret returns a Secret holding data.
func NewSecret(name string, data map[string]string) Secret { return Secret{Name: name, data: data} }

// SecretFromV1 reads a Secret off the API server; the client has already base64-decoded `data`.
func SecretFromV1(secret *corev1.Secret) Secret {
	data := make(map[string]string, len(secret.Data))
	for key, value := range secret.Data {
		data[key] = string(value)
	}
	return Secret{Name: secret.Name, data: data}
}

// Value returns the value of key, if the Secret holds it.
func (s Secret) Value(key string) (string, bool) {
	value, ok := s.data[key]
	return value, ok
}

func (Secret) String() string   { return "Secret{redacted}" }
func (Secret) GoString() string { return "Secret{redacted}" }
