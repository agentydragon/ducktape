package workloadidentity

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"fmt"

	"github.com/spiffe/go-spiffe/v2/bundle/x509bundle"
	"github.com/spiffe/go-spiffe/v2/proto/spiffe/workload"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
)

// ParsePEM converts cert-manager certificate, private-key, and trust-bundle PEM
// files into the DER-encoded X.509-SVID response required by the Workload API.
func ParsePEM(certPEM, keyPEM, bundlePEM []byte) (*workload.X509SVIDResponse, error) {
	pair, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return nil, fmt.Errorf("parse X.509 certificate and private key: %w", err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(pair.PrivateKey)
	if err != nil {
		return nil, fmt.Errorf("marshal X.509 private key: %w", err)
	}
	certificateDER := bytes.Join(pair.Certificate, nil)
	svid, err := x509svid.ParseRaw(certificateDER, keyDER)
	if err != nil {
		return nil, fmt.Errorf("parse X.509 SVID: %w", err)
	}
	bundle, err := x509bundle.Parse(svid.ID.TrustDomain(), bundlePEM)
	if err != nil {
		return nil, fmt.Errorf("parse X.509 trust-bundle PEM: %w", err)
	}
	response := &workload.X509SVIDResponse{Svids: []*workload.X509SVID{{
		SpiffeId:    svid.ID.String(),
		X509Svid:    certificateDER,
		X509SvidKey: keyDER,
		Bundle:      certificatesDER(bundle.X509Authorities()),
	}}}
	if _, err := validateResponse(response); err != nil {
		return nil, fmt.Errorf("validate parsed X.509 credentials: %w", err)
	}
	return response, nil
}

func certificatesDER(certificates []*x509.Certificate) []byte {
	var der []byte
	for _, certificate := range certificates {
		der = append(der, certificate.Raw...)
	}
	return der
}
