// Programmatic generation of Talos machine configs and client configs for
// QEMU integration tests. Parallels the kubespand agent config generation
// pattern (NewTestAgentConfig / CreateKubespandCIDATA) but for Talos VMs.
package qemu_tests

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/pem"
	"fmt"
	"math/big"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"

	sx509 "github.com/siderolabs/crypto/x509"
	v1alpha1 "github.com/siderolabs/talos/pkg/machinery/config/types/v1alpha1"
	"gopkg.in/yaml.v3"
)

// TestTalosSecrets holds all cryptographic material for a test Talos cluster.
// Generated once per test, shared across all node configs and the client config.
type TestTalosSecrets struct {
	MachineCA    *sx509.PEMEncodedCertificateAndKey
	ClusterCA    *sx509.PEMEncodedCertificateAndKey
	AggregatorCA *sx509.PEMEncodedCertificateAndKey
	EtcdCA       *sx509.PEMEncodedCertificateAndKey
	// ServiceAccountKey is a PEM-encoded RSA private key (no certificate).
	ServiceAccountKey []byte
	MachineToken      string
	ClusterToken      string
	ClusterID         string
	ClusterSecret     string
	SecretboxSecret   string
	// ClientCert is the admin client cert signed by MachineCA, for talosconfig.
	ClientCert *sx509.PEMEncodedCertificateAndKey
}

// GenerateTestTalosSecrets creates a complete set of cluster secrets for test
// Talos VMs. All crypto material is freshly generated — no committed testdata.
func GenerateTestTalosSecrets(t *testing.T) *TestTalosSecrets {
	t.Helper()

	// Machine CA (ed25519) — signs apid client certs.
	machineCA, err := sx509.NewSelfSignedCertificateAuthority(
		sx509.Organization("talos"),
	)
	if err != nil {
		t.Fatalf("generate machine CA: %v", err)
	}

	// Client cert for talosconfig (ed25519, signed by machine CA).
	clientKP, err := sx509.NewKeyPair(machineCA,
		sx509.Organization("os:admin"),
	)
	if err != nil {
		t.Fatalf("generate client cert: %v", err)
	}

	// Cluster CA (ECDSA P-256) — Kubernetes API server CA.
	clusterCA := generateECDSACA(t, "kubernetes")
	// Aggregator CA (ECDSA P-256) — front-proxy CA, empty subject.
	aggregatorCA := generateECDSACA(t, "")
	// Etcd CA (ECDSA P-256).
	etcdCA := generateECDSACA(t, "etcd")
	// Service account signing key (RSA).
	saKey := generateRSAKey(t)

	return &TestTalosSecrets{
		MachineCA:         sx509.NewCertificateAndKeyFromCertificateAuthority(machineCA),
		ClusterCA:         clusterCA,
		AggregatorCA:      aggregatorCA,
		EtcdCA:            etcdCA,
		ServiceAccountKey: saKey,
		MachineToken:      randomBootstrapToken(),
		ClusterToken:      randomBootstrapToken(),
		ClusterID:         RandomBase64(32),
		ClusterSecret:     RandomBase64(32),
		SecretboxSecret:   RandomBase64(32),
		ClientCert:        sx509.NewCertificateAndKeyFromKeyPair(clientKP),
	}
}

// Creds returns TestClusterCreds derived from these secrets, for use with
// NewTestAgentConfig when building kubespand configs.
func (s *TestTalosSecrets) Creds() TestClusterCreds {
	return TestClusterCreds{
		ClusterID:    s.ClusterID,
		SharedSecret: s.ClusterSecret,
		CACrt:        string(s.MachineCA.Crt),
		MachineToken: s.MachineToken,
	}
}

// TalosNodeConfig holds per-node parameters for Talos config generation.
type TalosNodeConfig struct {
	// IP is the primary interface address, e.g. "192.168.50.253/24".
	IP string
	// Gateway is the default gateway (optional, used for NAT workers).
	Gateway string
	// ControlPlaneEndpoint is the cluster API endpoint, e.g. "https://192.168.50.253:6443".
	ControlPlaneEndpoint string
	// DiscoveryEndpoint is the discovery service URL, e.g. "http://192.168.50.254:3000".
	DiscoveryEndpoint string
	// EndpointFilters are KubeSpan endpoint CIDR filters.
	EndpointFilters []string
	// CertSANs are extra SANs for the API server certificate.
	CertSANs []string
}

// ControlPlaneConfig returns a marshaled Talos controlplane machine config.
func (s *TestTalosSecrets) ControlPlaneConfig(opts TalosNodeConfig) []byte {
	cfg := s.baseConfig("controlplane", opts)
	// CPs have private keys for all CAs.
	cfg.ClusterConfig.ClusterCA = s.ClusterCA
	cfg.ClusterConfig.ClusterAggregatorCA = s.AggregatorCA
	cfg.ClusterConfig.ClusterServiceAccount = &sx509.PEMEncodedKey{Key: s.ServiceAccountKey}
	cfg.ClusterConfig.EtcdConfig = &v1alpha1.EtcdConfig{
		RootCA: s.EtcdCA,
	}
	// API server cert SANs (needed for Talos API trust, not K8s workloads).
	if len(opts.CertSANs) > 0 {
		cfg.ClusterConfig.APIServerConfig = &v1alpha1.APIServerConfig{
			CertSANs: opts.CertSANs,
		}
	}

	return marshalConfig(cfg)
}

// WorkerConfig returns a marshaled Talos worker machine config.
func (s *TestTalosSecrets) WorkerConfig(opts TalosNodeConfig) []byte {
	cfg := s.baseConfig("worker", opts)
	// Workers only have public certs (no private keys).
	cfg.MachineConfig.MachineCA = &sx509.PEMEncodedCertificateAndKey{
		Crt: s.MachineCA.Crt,
		Key: []byte{},
	}
	cfg.ClusterConfig.ClusterCA = &sx509.PEMEncodedCertificateAndKey{
		Crt: s.ClusterCA.Crt,
		Key: []byte{},
	}
	// Workers don't have aggregator CA, service account, etcd CA, or apiServer config.
	return marshalConfig(cfg)
}

// WriteTalosconfig writes a talosconfig (client config) file and returns the path.
func (s *TestTalosSecrets) WriteTalosconfig(t *testing.T, dir string) string {
	t.Helper()
	// talosconfig format: YAML with context, ca, crt, key as base64.
	config := map[string]interface{}{
		"context": "test-kubespan",
		"contexts": map[string]interface{}{
			"test-kubespan": map[string]interface{}{
				"endpoints": []string{},
				"ca":        base64.StdEncoding.EncodeToString(s.MachineCA.Crt),
				"crt":       base64.StdEncoding.EncodeToString(s.ClientCert.Crt),
				"key":       base64.StdEncoding.EncodeToString(s.ClientCert.Key),
			},
		},
	}
	data, err := yaml.Marshal(config)
	if err != nil {
		t.Fatalf("marshal talosconfig: %v", err)
	}
	path := filepath.Join(dir, "talosconfig")
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatalf("write talosconfig: %v", err)
	}
	return path
}

// baseConfig constructs the shared v1alpha1.Config fields common to CPs and workers.
func (s *TestTalosSecrets) baseConfig(machineType string, opts TalosNodeConfig) *v1alpha1.Config {
	// Build network interfaces.
	eth0 := &v1alpha1.Device{
		DeviceInterface: "eth0",
		DeviceAddresses: []string{opts.IP},
	}
	if opts.Gateway != "" {
		eth0.DeviceRoutes = []*v1alpha1.Route{{
			RouteNetwork: "0.0.0.0/0",
			RouteGateway: opts.Gateway,
		}}
	}
	// Management NIC (QEMU user-mode networking).
	eth1 := &v1alpha1.Device{
		DeviceInterface: "eth1",
		DeviceAddresses: []string{"10.0.2.15/24"},
	}

	kubeSpan := &v1alpha1.NetworkKubeSpan{
		KubeSpanEnabled: boolPtr(true),
	}
	if len(opts.EndpointFilters) > 0 {
		kubeSpan.KubeSpanFilters = &v1alpha1.KubeSpanFilters{
			KubeSpanFiltersEndpoints: opts.EndpointFilters,
		}
	}

	trueVal := true
	cfg := &v1alpha1.Config{
		ConfigVersion: "v1alpha1",
		ConfigPersist: &trueVal,
		MachineConfig: &v1alpha1.MachineConfig{
			MachineType:     machineType,
			MachineToken:    s.MachineToken,
			MachineCA:       s.MachineCA,
			MachineCertSANs: []string{"127.0.0.1"},
			MachineKubelet:  &v1alpha1.KubeletConfig{},
			MachineNetwork: &v1alpha1.NetworkConfig{
				NetworkInterfaces: []*v1alpha1.Device{eth0, eth1},
				NetworkKubeSpan:   kubeSpan,
			},
			MachineInstall: &v1alpha1.InstallConfig{
				InstallDisk: "/dev/sda",
			},
			MachineFeatures: &v1alpha1.FeaturesConfig{
				RBAC:                 &trueVal,
				StableHostname:       &trueVal,
				ApidCheckExtKeyUsage: &trueVal,
				DiskQuotaSupport:     &trueVal,
				KubePrismSupport: &v1alpha1.KubePrism{
					ServerEnabled: boolPtr(true),
					ServerPort:    7445,
				},
				HostDNSSupport: &v1alpha1.HostDNSConfig{
					HostDNSEnabled:              boolPtr(true),
					HostDNSForwardKubeDNSToHost: boolPtr(true),
				},
			},
		},
		ClusterConfig: &v1alpha1.ClusterConfig{
			ClusterID:     s.ClusterID,
			ClusterSecret: s.ClusterSecret,
			ControlPlane: &v1alpha1.ControlPlaneConfig{
				Endpoint: &v1alpha1.Endpoint{URL: mustParseURL(opts.ControlPlaneEndpoint)},
			},
			ClusterName:                      "test-kubespan",
			BootstrapToken:                   s.ClusterToken,
			ClusterSecretboxEncryptionSecret: s.SecretboxSecret,
			ClusterNetwork: &v1alpha1.ClusterNetworkConfig{
				CNI: &v1alpha1.CNIConfig{
					CNIName: "none",
				},
				DNSDomain:     "cluster.local",
				PodSubnet:     []string{"10.244.0.0/16"},
				ServiceSubnet: []string{"10.96.0.0/12"},
			},
			ClusterDiscoveryConfig: &v1alpha1.ClusterDiscoveryConfig{
				DiscoveryEnabled: boolPtr(true),
				DiscoveryRegistries: v1alpha1.DiscoveryRegistriesConfig{
					RegistryKubernetes: v1alpha1.RegistryKubernetesConfig{
						RegistryDisabled: boolPtr(true),
					},
					RegistryService: v1alpha1.RegistryServiceConfig{
						RegistryEndpoint: opts.DiscoveryEndpoint,
					},
				},
			},
		},
	}

	return cfg
}

func marshalConfig(cfg *v1alpha1.Config) []byte {
	data, err := yaml.Marshal(cfg)
	if err != nil {
		panic(fmt.Sprintf("marshal talos config: %v", err))
	}
	return data
}

func mustParseURL(rawURL string) *url.URL {
	u, err := url.Parse(rawURL)
	if err != nil {
		panic(fmt.Sprintf("parse URL %q: %v", rawURL, err))
	}
	return u
}

// generateECDSACA creates a self-signed ECDSA P-256 CA certificate.
func generateECDSACA(t *testing.T, org string) *sx509.PEMEncodedCertificateAndKey {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate ECDSA key: %v", err)
	}
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	template := &x509.Certificate{
		SerialNumber:          serial,
		NotBefore:             time.Now(),
		NotAfter:              time.Now().Add(10 * 365 * 24 * time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
		IsCA:                  true,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
	}
	if org != "" {
		template.Subject = pkix.Name{Organization: []string{org}}
	}
	certDER, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("create ECDSA cert: %v", err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("marshal ECDSA key: %v", err)
	}
	return &sx509.PEMEncodedCertificateAndKey{
		Crt: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certDER}),
		Key: pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}),
	}
}

// generateRSAKey creates a PEM-encoded RSA private key for service account signing.
func generateRSAKey(t *testing.T) []byte {
	t.Helper()
	rsaKey, err := sx509.NewRSAKey()
	if err != nil {
		t.Fatalf("generate RSA key: %v", err)
	}
	return rsaKey.GetPrivateKeyPEM()
}

func randomBootstrapToken() string {
	a := make([]byte, 3)
	b := make([]byte, 8)
	rand.Read(a)
	rand.Read(b)
	return fmt.Sprintf("%x.%x", a, b)
}

func boolPtr(v bool) *bool { return &v }
