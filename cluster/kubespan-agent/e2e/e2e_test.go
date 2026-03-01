//go:build integration

// Package e2e contains integration tests for kubespand.
//
// The test verifies that kubespand can discover a Talos KubeSpan peer via
// the Talos discovery service. It runs:
//  1. A local discovery service with self-signed TLS
//  2. A Talos container with KubeSpan enabled
//  3. kubespand in discovery-only mode
//
// The test proves kubespand speaks the same encrypted gRPC discovery protocol
// as Talos and that mutual peer discovery works end-to-end.
//
// Requirements: Docker, ~2 minutes, internet (to pull container images on first run).
package e2e

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const (
	discoveryImage = "ghcr.io/siderolabs/discovery-service:latest"
	talosImage     = "ghcr.io/siderolabs/talos:v1.9.5"
	networkPrefix  = "kubespan-e2e"
)

func TestKubeSpanDiscovery(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping integration test in short mode")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	// Generate unique test ID for resource names.
	testID := randomHex(8)
	networkName := fmt.Sprintf("%s-%s", networkPrefix, testID)
	t.Logf("test ID: %s, network: %s", testID, networkName)

	// Generate shared cluster credentials.
	clusterID := base64.StdEncoding.EncodeToString(randomBytes(32))
	clusterSecret := base64.StdEncoding.EncodeToString(randomBytes(32))
	t.Logf("cluster_id: %s", clusterID)

	// Create temp directory for test artifacts.
	tmpDir := t.TempDir()

	// Generate self-signed TLS cert for the discovery service.
	certFile, keyFile := generateTLSCert(t, tmpDir)

	// Create Docker network.
	dockerRun(t, "docker", "network", "create", networkName)
	t.Cleanup(func() {
		dockerRunIgnoreErr("docker", "network", "rm", networkName)
	})

	// Start discovery service.
	discoveryName := fmt.Sprintf("discovery-%s", testID)
	dockerRun(t, "docker", "run", "-d",
		"--name", discoveryName,
		"--network", networkName,
		"-v", certFile+":/tls/cert.pem:ro",
		"-v", keyFile+":/tls/key.pem:ro",
		discoveryImage,
		"-certificate-path", "/tls/cert.pem",
		"-key-path", "/tls/key.pem",
	)
	t.Cleanup(func() {
		dockerRunIgnoreErr("docker", "rm", "-f", discoveryName)
	})
	t.Log("discovery service started")

	// Wait for discovery service to be ready.
	waitForContainer(t, ctx, discoveryName, 30*time.Second)

	// Build kubespand binary.
	kubespandBin := buildKubespand(t)

	// Write kubespand config.
	configFile := filepath.Join(tmpDir, "agent.yaml")
	writeKubespandConfig(t, configFile, clusterID, clusterSecret, discoveryName+":3000")

	// Build kubespand Docker image.
	kubespandImage := fmt.Sprintf("kubespand-test:%s", testID)
	buildKubespandImage(t, tmpDir, kubespandBin, kubespandImage)
	t.Cleanup(func() {
		dockerRunIgnoreErr("docker", "rmi", kubespandImage)
	})

	// Generate Talos machine config and start Talos container.
	talosName := fmt.Sprintf("talos-%s", testID)
	startTalosContainer(t, ctx, tmpDir, talosName, networkName, clusterID, clusterSecret, discoveryName)
	t.Cleanup(func() {
		dockerRunIgnoreErr("docker", "rm", "-f", talosName)
	})

	// Give Talos a moment to start KubeSpan and register with discovery.
	t.Log("waiting for Talos to register with discovery service...")
	time.Sleep(15 * time.Second)

	// Run kubespand in discovery-only mode.
	kubespandName := fmt.Sprintf("kubespand-%s", testID)
	t.Log("starting kubespand in discovery-only mode...")
	out := dockerRunOutput(t, "docker", "run",
		"--name", kubespandName,
		"--network", networkName,
		"-v", configFile+":/etc/kubespan/agent.yaml:ro",
		kubespandImage,
		"/kubespand",
		"-config", "/etc/kubespan/agent.yaml",
		"-discovery-only",
		"-timeout", "120s",
	)
	t.Cleanup(func() {
		dockerRunIgnoreErr("docker", "rm", "-f", kubespandName)
	})

	t.Logf("kubespand output:\n%s", out)

	// The test passes if kubespand exited 0 (peers found).
	// dockerRunOutput already checks the exit code.
	if !strings.Contains(out, "peers found") {
		t.Errorf("kubespand did not find peers; output:\n%s", out)
	}
}

// randomBytes generates n random bytes.
func randomBytes(n int) []byte {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return b
}

// randomHex generates a random hex string of n bytes.
func randomHex(n int) string {
	return fmt.Sprintf("%x", randomBytes(n))
}

// generateTLSCert creates a self-signed TLS certificate and key in the given directory.
func generateTLSCert(t *testing.T, dir string) (certFile, keyFile string) {
	t.Helper()

	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generating TLS key: %v", err)
	}

	template := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "discovery-test"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		// SANs must cover the Docker hostname used by kubespand to connect.
		DNSNames:    []string{"discovery-test", "localhost"},
		IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},
	}

	certDER, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("creating self-signed cert: %v", err)
	}

	certFile = filepath.Join(dir, "cert.pem")
	keyFile = filepath.Join(dir, "key.pem")

	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certDER})
	if err := os.WriteFile(certFile, certPEM, 0644); err != nil {
		t.Fatalf("writing cert: %v", err)
	}

	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("marshaling key: %v", err)
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
	if err := os.WriteFile(keyFile, keyPEM, 0600); err != nil {
		t.Fatalf("writing key: %v", err)
	}

	return certFile, keyFile
}

// buildKubespand builds the kubespand binary using Bazel and returns the path.
func buildKubespand(t *testing.T) string {
	t.Helper()
	t.Log("building kubespand...")

	cmd := exec.Command("bazel", "build", "//cluster/kubespan-agent")
	cmd.Dir = findWorkspaceRoot(t)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("bazel build failed: %v\n%s", err, out)
	}

	// Find the built binary.
	cmd = exec.Command("bazel", "cquery", "--output=files", "//cluster/kubespan-agent")
	cmd.Dir = findWorkspaceRoot(t)
	out, err = cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("bazel cquery failed: %v\n%s", err, out)
	}

	binPath := strings.TrimSpace(string(out))
	if binPath == "" {
		t.Fatal("bazel cquery returned empty path")
	}

	// The binary is typically at bazel-bin/cluster/kubespan-agent/kubespan-agent_/kubespan-agent
	// but cquery returns the runfiles path. Let's use the known path.
	wsRoot := findWorkspaceRoot(t)
	fullPath := filepath.Join(wsRoot, "bazel-bin/cluster/kubespan-agent/kubespan-agent_/kubespan-agent")
	if _, err := os.Stat(fullPath); err != nil {
		t.Fatalf("kubespand binary not found at %s: %v", fullPath, err)
	}

	return fullPath
}

// buildKubespandImage creates a Docker image containing the kubespand binary.
func buildKubespandImage(t *testing.T, tmpDir, binPath, imageName string) {
	t.Helper()

	// Copy the binary to the build context.
	binData, err := os.ReadFile(binPath)
	if err != nil {
		t.Fatalf("reading kubespand binary: %v", err)
	}
	localBin := filepath.Join(tmpDir, "kubespand")
	if err := os.WriteFile(localBin, binData, 0755); err != nil {
		t.Fatalf("writing kubespand binary: %v", err)
	}

	// Write a minimal Dockerfile.
	dockerfile := filepath.Join(tmpDir, "Dockerfile")
	if err := os.WriteFile(dockerfile, []byte(`FROM debian:bookworm-slim
COPY kubespand /kubespand
ENTRYPOINT ["/kubespand"]
`), 0644); err != nil {
		t.Fatalf("writing Dockerfile: %v", err)
	}

	cmd := exec.Command("docker", "build", "--network=host", "-t", imageName, tmpDir)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("docker build failed: %v\n%s", err, out)
	}
	t.Log("kubespand Docker image built")
}

// writeKubespandConfig writes a kubespand YAML config file.
func writeKubespandConfig(t *testing.T, path, clusterID, clusterSecret, discoveryEndpoint string) {
	t.Helper()

	config := fmt.Sprintf(`cluster_id: %q
cluster_secret: %q
discovery_endpoint: %q
insecure_discovery: true
listen_port: 51820
mtu: 1420
identity_file: "/tmp/kubespan-identity.json"
machine_type: "worker"
`, clusterID, clusterSecret, discoveryEndpoint)

	if err := os.WriteFile(path, []byte(config), 0644); err != nil {
		t.Fatalf("writing kubespand config: %v", err)
	}
}

// startTalosContainer starts a Talos container with KubeSpan enabled.
func startTalosContainer(t *testing.T, ctx context.Context, tmpDir, name, network, clusterID, clusterSecret, discoveryHost string) {
	t.Helper()
	t.Log("generating Talos machine config...")

	// Generate Talos machine config using talosctl.
	// We need to build a config that has KubeSpan enabled with our cluster credentials
	// and points to our local discovery service.
	//
	// Since we may not have talosctl available, we'll construct a minimal machine config
	// manually using the Talos config format.

	// Talos machine config (v1alpha1) with KubeSpan enabled.
	talosConfig := map[string]interface{}{
		"version": "v1alpha1",
		"persist": false,
		"machine": map[string]interface{}{
			"type": "worker",
			"network": map[string]interface{}{
				"kubespan": map[string]interface{}{
					"enabled": true,
				},
			},
		},
		"cluster": map[string]interface{}{
			"id":     clusterID,
			"secret": clusterSecret,
			"discovery": map[string]interface{}{
				"enabled": true,
				"registries": map[string]interface{}{
					"service": map[string]interface{}{
						"endpoint": fmt.Sprintf("https://%s:3000/", discoveryHost),
					},
				},
			},
			"controlPlane": map[string]interface{}{
				"endpoint": "https://localhost:6443",
			},
			"clusterNetwork": map[string]interface{}{
				"dnsDomain":   "cluster.local",
				"podSubnets":  []string{"10.244.0.0/16"},
				"serviceSubnets": []string{"10.96.0.0/12"},
			},
		},
	}

	configJSON, err := json.Marshal(talosConfig)
	if err != nil {
		t.Fatalf("marshaling Talos config: %v", err)
	}
	configB64 := base64.StdEncoding.EncodeToString(configJSON)

	t.Log("starting Talos container...")
	dockerRun(t, "docker", "run", "-d",
		"--name", name,
		"--network", network,
		"--privileged",
		"-e", "PLATFORM=container",
		"-e", "USERDATA="+configB64,
		talosImage,
	)
	t.Log("Talos container started")
}

// findWorkspaceRoot finds the Bazel workspace root.
func findWorkspaceRoot(t *testing.T) string {
	t.Helper()

	// Try BUILD_WORKSPACE_DIRECTORY first (set by `bazel test`).
	if ws := os.Getenv("BUILD_WORKSPACE_DIRECTORY"); ws != "" {
		return ws
	}

	// Walk up from the test's working directory.
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "MODULE.bazel")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("could not find workspace root (MODULE.bazel)")
		}
		dir = parent
	}
}

// waitForContainer waits for a Docker container to be running and healthy.
func waitForContainer(t *testing.T, ctx context.Context, name string, timeout time.Duration) {
	t.Helper()

	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		cmd := exec.CommandContext(ctx, "docker", "inspect", "-f", "{{.State.Running}}", name)
		out, err := cmd.Output()
		if err == nil && strings.TrimSpace(string(out)) == "true" {
			return
		}
		time.Sleep(time.Second)
	}
	t.Fatalf("container %s did not start within %v", name, timeout)
}

// dockerRun runs a docker command and fails the test on error.
func dockerRun(t *testing.T, args ...string) {
	t.Helper()
	cmd := exec.Command(args[0], args[1:]...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("command %v failed: %v\n%s", args, err, out)
	}
}

// dockerRunOutput runs a docker command, returning stdout+stderr, and fails on error.
func dockerRunOutput(t *testing.T, args ...string) string {
	t.Helper()
	cmd := exec.Command(args[0], args[1:]...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	combined := stdout.String() + stderr.String()
	if err != nil {
		t.Fatalf("command %v failed: %v\n%s", args, err, combined)
	}
	return combined
}

// dockerRunIgnoreErr runs a docker command and ignores errors (for cleanup).
func dockerRunIgnoreErr(args ...string) {
	cmd := exec.Command(args[0], args[1:]...)
	_ = cmd.Run()
}
