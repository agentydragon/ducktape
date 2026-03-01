// Integration test for kubespand: verifies peer discovery against a real
// Talos container and local discovery service. Requires Docker, ~2 minutes.
package main

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

	"gopkg.in/yaml.v3"
)

const (
	discoveryRepoTag = "ghcr.io/siderolabs/discovery-service:latest"
	talosRepoTag     = "ghcr.io/siderolabs/talos:v1.9.5"
	kubespandRepoTag = "kubespand:latest"
	networkPrefix    = "kubespan-e2e"
)

func TestKubeSpanDiscovery(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping integration test in short mode")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	// Load all container images from Bazel tarballs.
	loadImage(t, "third_party/siderolabs/discovery_service_load/tarball.tar", discoveryRepoTag)
	loadImage(t, "third_party/siderolabs/talos_v1_9_5_load/tarball.tar", talosRepoTag)
	loadImage(t, "cluster/kubespan_agent/kubespand_load/tarball.tar", kubespandRepoTag)

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
		discoveryRepoTag,
		"-certificate-path", "/tls/cert.pem",
		"-key-path", "/tls/key.pem",
	)
	t.Cleanup(func() {
		dockerRunIgnoreErr("docker", "rm", "-f", discoveryName)
	})
	t.Log("discovery service started")

	// Wait for discovery service to be ready.
	waitForContainer(t, ctx, discoveryName, 30*time.Second)

	// Write kubespand config.
	configFile := filepath.Join(tmpDir, "agent.yaml")
	writeKubespandConfig(t, configFile, clusterID, clusterSecret, discoveryName+":3000")

	// Generate Talos machine config and start Talos container.
	talosName := fmt.Sprintf("talos-%s", testID)
	startTalosContainer(t, tmpDir, talosName, networkName, clusterID, clusterSecret, discoveryName)
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
		kubespandRepoTag,
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

// loadImage loads a container image tarball from a Bazel runfiles path.
func loadImage(t *testing.T, rlocation, repoTag string) {
	t.Helper()

	// Resolve the tarball path from runfiles.
	tarball := resolveRunfile(t, rlocation)

	t.Logf("loading image %s from %s", repoTag, tarball)
	cmd := exec.Command("docker", "load", "-i", tarball)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("docker load %s failed: %v\n%s", tarball, err, out)
	}
}

// resolveRunfile finds a file in Bazel runfiles or the execroot.
func resolveRunfile(t *testing.T, rlocation string) string {
	t.Helper()

	// Under `bazel test`, RUNFILES_DIR or TEST_SRCDIR is set.
	if dir := os.Getenv("RUNFILES_DIR"); dir != "" {
		p := filepath.Join(dir, "_main", rlocation)
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	if dir := os.Getenv("TEST_SRCDIR"); dir != "" {
		p := filepath.Join(dir, "_main", rlocation)
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}

	// Fallback: look relative to the test binary location.
	exe, err := os.Executable()
	if err == nil {
		runfilesDir := exe + ".runfiles"
		p := filepath.Join(runfilesDir, "_main", rlocation)
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}

	t.Fatalf("could not resolve runfile %q (RUNFILES_DIR=%q, TEST_SRCDIR=%q)", rlocation, os.Getenv("RUNFILES_DIR"), os.Getenv("TEST_SRCDIR"))
	return ""
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

// writeKubespandConfig writes a kubespand YAML config file using the Config struct.
func writeKubespandConfig(t *testing.T, path, clusterID, clusterSecret, discoveryEndpoint string) {
	t.Helper()

	cfg := Config{
		ClusterID:         clusterID,
		ClusterSecret:     clusterSecret,
		DiscoveryEndpoint: discoveryEndpoint,
		InsecureDiscovery: true,
		ListenPort:        51820,
		MTU:               1420,
		IdentityFile:      "/tmp/kubespan-identity.json",
		MachineType:       "worker",
	}

	data, err := yaml.Marshal(cfg)
	if err != nil {
		t.Fatalf("marshaling kubespand config: %v", err)
	}

	if err := os.WriteFile(path, data, 0644); err != nil {
		t.Fatalf("writing kubespand config: %v", err)
	}
}

// startTalosContainer starts a Talos container with KubeSpan enabled.
func startTalosContainer(t *testing.T, tmpDir, name, network, clusterID, clusterSecret, discoveryHost string) {
	t.Helper()
	t.Log("generating Talos machine config...")

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
				"dnsDomain":      "cluster.local",
				"podSubnets":     []string{"10.244.0.0/16"},
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
		talosRepoTag,
	)
	t.Log("Talos container started")
}

// waitForContainer waits for a Docker container to be running.
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
