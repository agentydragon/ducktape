package workloadidentity

import (
	"bytes"
	"crypto"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"

	"github.com/spiffe/go-spiffe/v2/proto/spiffe/workload"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
)

// ParsePEM converts cert-manager certificate, private-key, and trust-bundle PEM
// files into the DER-encoded X.509-SVID response required by the Workload API.
func ParsePEM(certPEM, keyPEM, bundlePEM []byte) (*workload.X509SVIDResponse, error) {
	certificates, err := parseCertificates(certPEM)
	if err != nil {
		return nil, fmt.Errorf("parse X.509 certificate PEM: %w", err)
	}
	keyDER, err := parsePrivateKey(keyPEM)
	if err != nil {
		return nil, fmt.Errorf("parse X.509 private-key PEM: %w", err)
	}
	bundleCertificates, err := parseCertificates(bundlePEM)
	if err != nil {
		return nil, fmt.Errorf("parse X.509 trust-bundle PEM: %w", err)
	}
	identity, err := x509svid.IDFromCert(certificates[0])
	if err != nil {
		return nil, fmt.Errorf("read SPIFFE ID from leaf certificate: %w", err)
	}
	response := &workload.X509SVIDResponse{Svids: []*workload.X509SVID{{
		SpiffeId:    identity.String(),
		X509Svid:    certificatesDER(certificates),
		X509SvidKey: keyDER,
		Bundle:      certificatesDER(bundleCertificates),
	}}}
	if _, err := validateResponse(response); err != nil {
		return nil, fmt.Errorf("validate parsed X.509 credentials: %w", err)
	}
	return response, nil
}

func parseCertificates(data []byte) ([]*x509.Certificate, error) {
	remaining := bytes.TrimSpace(data)
	if len(remaining) == 0 {
		return nil, errors.New("certificate PEM is empty")
	}
	certificates := make([]*x509.Certificate, 0, 2)
	for len(remaining) > 0 {
		if !bytes.HasPrefix(remaining, []byte("-----BEGIN ")) {
			return nil, errors.New("certificate PEM has non-PEM content")
		}
		block, rest := pem.Decode(remaining)
		if block == nil || block.Type != "CERTIFICATE" || len(block.Headers) != 0 {
			return nil, errors.New("certificate PEM must contain only CERTIFICATE blocks")
		}
		certificate, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("parse certificate block: %w", err)
		}
		certificates = append(certificates, certificate)
		remaining = bytes.TrimSpace(rest)
	}
	return certificates, nil
}

func parsePrivateKey(data []byte) ([]byte, error) {
	remaining := bytes.TrimSpace(data)
	if !bytes.HasPrefix(remaining, []byte("-----BEGIN ")) {
		return nil, errors.New("private-key PEM is empty or has non-PEM content")
	}
	block, rest := pem.Decode(remaining)
	if block == nil || len(bytes.TrimSpace(rest)) != 0 || len(block.Headers) != 0 {
		return nil, errors.New("private-key PEM must contain exactly one unencrypted key block")
	}
	var key any
	var err error
	switch block.Type {
	case "PRIVATE KEY":
		key, err = x509.ParsePKCS8PrivateKey(block.Bytes)
	case "RSA PRIVATE KEY":
		key, err = x509.ParsePKCS1PrivateKey(block.Bytes)
	case "EC PRIVATE KEY":
		key, err = x509.ParseECPrivateKey(block.Bytes)
	default:
		return nil, fmt.Errorf("unsupported private-key PEM type %q", block.Type)
	}
	if err != nil {
		return nil, fmt.Errorf("parse %s: %w", block.Type, err)
	}
	if _, ok := key.(crypto.Signer); !ok {
		return nil, fmt.Errorf("private key type %T cannot sign", key)
	}
	return x509.MarshalPKCS8PrivateKey(key)
}

func certificatesDER(certificates []*x509.Certificate) []byte {
	var der []byte
	for _, certificate := range certificates {
		der = append(der, certificate.Raw...)
	}
	return der
}
