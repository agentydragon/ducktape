// Programmatic generation of Talos machine configs and client configs for
// QEMU integration tests. Parallels the kubespand agent config generation
// pattern (NewTestAgentConfig / CreateKubespandCIDATA) but for Talos VMs.
package qemu_tests

import (
	"crypto/rand"
	"fmt"
	"net/url"
	"testing"
	"time"

	sx509 "github.com/siderolabs/crypto/x509"
	clientconfig "github.com/siderolabs/talos/pkg/machinery/client/config"
	"github.com/siderolabs/talos/pkg/machinery/config"
	gensecrets "github.com/siderolabs/talos/pkg/machinery/config/generate/secrets"
	v1alpha1 "github.com/siderolabs/talos/pkg/machinery/config/types/v1alpha1"
	"github.com/siderolabs/talos/pkg/machinery/role"
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
// Talos VMs. Uses Talos's own CA and certificate generation functions to ensure
// crypto material matches what real Talos clusters produce.
// Ref: pkg/machinery/config/generate/secrets/bundle.go (Bundle.populate)
// Ref: pkg/machinery/config/generate/secrets/ca.go (NewAdminCertificateAndKey)
func GenerateTestTalosSecrets(t *testing.T) *TestTalosSecrets {
	t.Helper()

	now := time.Now()
	contract := config.TalosVersionCurrent

	// All CA generation uses Talos's own functions from
	// pkg/machinery/config/generate/secrets/ca.go.
	machineCA, err := gensecrets.NewTalosCA(now)
	if err != nil {
		t.Fatalf("generate machine CA: %v", err)
	}
	machineCAPEM := sx509.NewCertificateAndKeyFromCertificateAuthority(machineCA)

	// Admin client cert with ExtKeyUsage=[ClientAuth] only.
	clientCert, err := gensecrets.NewAdminCertificateAndKey(
		now, machineCAPEM, role.MakeSet(role.Admin), 365*24*time.Hour,
	)
	if err != nil {
		t.Fatalf("generate admin client cert: %v", err)
	}

	clusterCA, err := gensecrets.NewKubernetesCA(now, contract)
	if err != nil {
		t.Fatalf("generate kubernetes CA: %v", err)
	}
	aggregatorCA, err := gensecrets.NewAggregatorCA(now, contract)
	if err != nil {
		t.Fatalf("generate aggregator CA: %v", err)
	}
	etcdCA, err := gensecrets.NewEtcdCA(now, contract)
	if err != nil {
		t.Fatalf("generate etcd CA: %v", err)
	}

	return &TestTalosSecrets{
		MachineCA:         machineCAPEM,
		ClusterCA:         sx509.NewCertificateAndKeyFromCertificateAuthority(clusterCA),
		AggregatorCA:      sx509.NewCertificateAndKeyFromCertificateAuthority(aggregatorCA),
		EtcdCA:            sx509.NewCertificateAndKeyFromCertificateAuthority(etcdCA),
		ServiceAccountKey: generateRSAKey(t),
		MachineToken:      randomBootstrapToken(),
		ClusterToken:      randomBootstrapToken(),
		ClusterID:         RandomBase64(32),
		ClusterSecret:     RandomBase64(32),
		SecretboxSecret:   RandomBase64(32),
		ClientCert:        clientCert,
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

// WriteTalosconfig writes a talosconfig (client config) file to path.
// Ref: pkg/machinery/client/config/config.go (NewConfig + Save)
func (s *TestTalosSecrets) WriteTalosconfig(t *testing.T, path string) {
	t.Helper()
	cfg := clientconfig.NewConfig("test-kubespan", nil, s.MachineCA.Crt, s.ClientCert)
	if err := cfg.Save(path); err != nil {
		t.Fatalf("write talosconfig: %v", err)
	}
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
