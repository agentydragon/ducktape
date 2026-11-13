// Package workloadid encodes and parses canonical Agentplane workload SPIFFE identities.
package workloadid

import (
	"errors"
	"fmt"
	"strings"

	"github.com/spiffe/go-spiffe/v2/spiffeid"
)

var trustDomain = spiffeid.RequireTrustDomainFromString("agentplane")

// ID is the identity a WorkloadCredential's certificate carries: the record's namespace and
// name, which pick the record, and the credential ID its status must still hold.
type ID struct {
	Namespace, Name, CredentialID string
}

// URI is spiffe://agentplane/<namespace>/<name>/<credential-id>.
func (w ID) URI() (string, error) {
	if err := validateNamespace(w.Namespace); err != nil {
		return "", err
	}
	if !isDNSSubdomain(w.Name) {
		return "", errors.New("workload name must be a lowercase DNS subdomain")
	}
	// The issuer mints 32 hexadecimal digits; a verifier compares the whole URI with its record's.
	if len(w.CredentialID) > 63 || !isDNSSubdomain(w.CredentialID) || strings.Contains(w.CredentialID, ".") {
		return "", errors.New("credential ID must be a lowercase DNS label")
	}
	id, err := spiffeid.FromSegments(trustDomain, w.Namespace, w.Name, w.CredentialID)
	if err != nil {
		return "", fmt.Errorf("workload identity: %w", err)
	}
	return id.String(), nil
}

// Parse accepts only the canonical form URI writes.
func Parse(raw string) (ID, error) {
	id, err := spiffeid.FromString(raw)
	if err != nil {
		return ID{}, fmt.Errorf("parse workload identity URI: %w", err)
	}
	if !id.MemberOf(trustDomain) {
		return ID{}, errors.New("workload identity URI must use the agentplane SPIFFE trust domain")
	}
	segments := strings.Split(strings.TrimPrefix(id.Path(), "/"), "/")
	if len(segments) != 3 {
		return ID{}, errors.New("workload identity URI must contain namespace, name and credential ID")
	}
	workload := ID{Namespace: segments[0], Name: segments[1], CredentialID: segments[2]}
	canonical, err := workload.URI()
	if err != nil {
		return ID{}, err
	}
	if canonical != raw {
		return ID{}, errors.New("workload identity URI is not canonical")
	}
	return workload, nil
}

func validateNamespace(namespace string) error {
	if len(namespace) == 0 || len(namespace) > 63 {
		return errors.New("namespace must be 1 to 63 characters")
	}
	for _, r := range namespace {
		if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-') {
			return errors.New("namespace must be a lowercase DNS label")
		}
	}
	if namespace[0] == '-' || namespace[len(namespace)-1] == '-' {
		return errors.New("namespace must not start or end with a hyphen")
	}
	return nil
}

func isDNSSubdomain(name string) bool {
	if len(name) == 0 || len(name) > 253 {
		return false
	}
	for _, label := range strings.Split(name, ".") {
		if label == "" || label[0] == '-' || label[len(label)-1] == '-' {
			return false
		}
		for _, r := range label {
			if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-') {
				return false
			}
		}
	}
	return true
}
