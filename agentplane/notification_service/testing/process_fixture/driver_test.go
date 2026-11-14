package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/x509"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/kubeclient"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	processConfigEnv = "DUCKTAPE_NOTIFICATION_PROCESS_CONFIG"
	testNamespace    = "notification-process-test"
	testAudience     = "notification-process-test-audience"
)

// processConfig is the explicit test-only boundary between the owning fixture and the child service process.
type processConfig struct {
	DatabaseURL      string            `json:"database_url"`
	KubeconfigPath   string            `json:"kubeconfig_path"`
	Namespace        string            `json:"namespace"`
	TokenAudience    string            `json:"token_audience"`
	ServerCertFile   string            `json:"server_cert_file"`
	ServerKeyFile    string            `json:"server_key_file"`
	ClientCAFile     string            `json:"client_ca_file"`
	PeerTokenFile    string            `json:"peer_token_file"`
	SeedSubscription *subscriptionSeed `json:"seed_subscription,omitempty"`
}

type subscriptionSeed struct {
	SandboxName  string `json:"sandbox_name"`
	SandboxUID   string `json:"sandbox_uid"`
	IdentityName string `json:"identity_name"`
	IdentityUID  string `json:"identity_uid"`
}

type seededSubscription struct {
	ID             string `json:"id"`
	IdempotencyKey string `json:"idempotency_key"`
	RequestID      string `json:"request_id"`
}

type processReady struct {
	Address      string              `json:"grpc_address"`
	Subscription *seededSubscription `json:"subscription,omitempty"`
}

func TestProductionNotificationProcessListsOwnedSubscription(t *testing.T) {
	database, err := postgres.Start(t.Context(), migrations.Up)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := database.Close(); err != nil {
			t.Errorf("stop PostgreSQL fixture: %v", err)
		}
	})
	databaseURL := database.Clone(t)

	apiServer, err := kubeapiserver.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := apiServer.Stop(); err != nil {
			t.Errorf("stop envtest API server: %v", err)
		}
	})

	workload := workloadauthtesting.NewIdentityWorkload(t, apiServer, testNamespace, "notification-owner", testNamespace)
	sandbox := workload.Sandbox.DeepCopy()
	sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: workload.Identity.Name}
	if err := workload.Objects.Update(t.Context(), sandbox); err != nil {
		t.Fatal(err)
	}
	workload.Sandbox = sandbox

	ca := peertlstesting.NewCA(t)
	serverTLS := ca.IssueWithUsage(t, peertls.NotificationsURI, x509.ExtKeyUsageServerAuth)
	clientTLS, err := ca.IssueWithUsage(t, workload.URI, x509.ExtKeyUsageClientAuth).ClientCredentials(peertls.NotificationsURI)
	if err != nil {
		t.Fatal(err)
	}

	kubeconfigPath := writeKubeconfig(t, apiServer.Config)
	peerTokenFile := filepath.Join(t.TempDir(), "peer-token")
	if err := os.WriteFile(peerTokenFile, []byte("unused-test-peer-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	driverConfigPath := filepath.Join(t.TempDir(), "notification-process.json")
	encodedConfig, err := json.Marshal(processConfig{
		DatabaseURL:    databaseURL,
		KubeconfigPath: kubeconfigPath,
		Namespace:      testNamespace,
		TokenAudience:  testAudience,
		ServerCertFile: serverTLS.CertFile,
		ServerKeyFile:  serverTLS.KeyFile,
		ClientCAFile:   ca.RootFile,
		PeerTokenFile:  peerTokenFile,
		SeedSubscription: &subscriptionSeed{
			SandboxName: sandbox.Name, SandboxUID: string(sandbox.UID),
			IdentityName: workload.Identity.Name, IdentityUID: string(workload.Identity.UID),
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(driverConfigPath, encodedConfig, 0o600); err != nil {
		t.Fatal(err)
	}
	process := startNotificationProcess(t, driverConfigPath)
	connection, err := grpc.NewClient(process.address, grpc.WithTransportCredentials(clientTLS))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := connection.Close(); err != nil {
			t.Errorf("close Notification Service client: %v", err)
		}
	})

	client := notificationspb.NewNotificationsClient(connection)
	var listed *notificationspb.ListSubscriptionsResponse
	deadline := time.Now().Add(20 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		callCtx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
		listed, lastErr = client.ListSubscriptions(callCtx, &notificationspb.ListSubscriptionsRequest{})
		cancel()
		if lastErr == nil {
			break
		}
		select {
		case <-process.done:
			t.Fatalf("Notification Service process exited before ListSubscriptions succeeded: %v; stderr artifact: %s", process.waitErr, process.stderrPath)
		case <-time.After(100 * time.Millisecond):
		}
	}
	if lastErr != nil {
		process.stop()
		t.Fatalf("ListSubscriptions over the caller SVID: %v; stderr artifact: %s", lastErr, process.stderrPath)
	}
	if len(listed.Subscriptions) != 1 {
		t.Fatalf("ListSubscriptions returned %d subscriptions, want the one preseeded row: %+v", len(listed.Subscriptions), listed.Subscriptions)
	}
	actual := listed.Subscriptions[0]
	created := process.subscription
	if created == nil {
		t.Fatal("Notification driver did not return its seeded subscription")
	}
	if actual.GetId() != created.ID || actual.GetIdempotencyKey() != created.IdempotencyKey || actual.GetSource().GetActions().GetRequestId() != created.RequestID {
		t.Fatalf("ListSubscriptions returned row %s (%q, %q), want owned row %s (%q, %q)",
			actual.GetId(), actual.GetIdempotencyKey(), actual.GetSource().GetActions().GetRequestId(),
			created.ID, created.IdempotencyKey, created.RequestID)
	}

	unknownTLS, err := ca.IssueWithUsage(t, "spiffe://agentplane/notification-process-test/unknown-owner/not-a-record", x509.ExtKeyUsageClientAuth).ClientCredentials(peertls.NotificationsURI)
	if err != nil {
		t.Fatal(err)
	}
	unknownConnection, err := grpc.NewClient(process.address, grpc.WithTransportCredentials(unknownTLS))
	if err != nil {
		t.Fatal(err)
	}
	defer unknownConnection.Close()
	callCtx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	_, err = notificationspb.NewNotificationsClient(unknownConnection).ListSubscriptions(callCtx, &notificationspb.ListSubscriptionsRequest{})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("an unrecorded trusted SVID got %v, want Unauthenticated", err)
	}

	rotatedURI := workload.Issue(t, strings.ReplaceAll(uuid.NewString(), "-", ""))
	_, err = client.ListSubscriptions(callCtx, &notificationspb.ListSubscriptionsRequest{})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("rotated workload credential still accepted its old SVID: %v", err)
	}
	rotatedTLS, err := ca.IssueWithUsage(t, rotatedURI, x509.ExtKeyUsageClientAuth).ClientCredentials(peertls.NotificationsURI)
	if err != nil {
		t.Fatal(err)
	}
	rotatedConnection, err := grpc.NewClient(process.address, grpc.WithTransportCredentials(rotatedTLS))
	if err != nil {
		t.Fatal(err)
	}
	defer rotatedConnection.Close()
	rotated, err := notificationspb.NewNotificationsClient(rotatedConnection).ListSubscriptions(callCtx, &notificationspb.ListSubscriptionsRequest{})
	if err != nil || len(rotated.GetSubscriptions()) != 1 || rotated.GetSubscriptions()[0].GetId() != created.ID {
		t.Fatalf("rotated SVID did not read its owned subscription: %v, %v", rotated, err)
	}
}

// TestNotificationProcessDriver runs only in the child copy of this test binary. Missing configuration is a test
// failure, so this driver cannot silently skip when someone targets it directly.
func TestNotificationProcessDriver(t *testing.T) {
	path := os.Getenv(processConfigEnv)
	if path == "" {
		t.Fatalf("%s is required", processConfigEnv)
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(contents))
	decoder.DisallowUnknownFields()
	var input processConfig
	if err := decoder.Decode(&input); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		t.Fatalf("driver configuration must contain one JSON object, got trailing decode error %v", err)
	}
	for name, value := range map[string]string{
		"database_url": input.DatabaseURL, "kubeconfig_path": input.KubeconfigPath, "namespace": input.Namespace,
		"token_audience": input.TokenAudience, "server_cert_file": input.ServerCertFile, "server_key_file": input.ServerKeyFile,
		"client_ca_file": input.ClientCAFile, "peer_token_file": input.PeerTokenFile,
	} {
		if strings.TrimSpace(value) == "" {
			t.Fatalf("driver configuration field %q is required", name)
		}
	}
	if input.SeedSubscription != nil {
		for name, value := range map[string]string{
			"sandbox_name": input.SeedSubscription.SandboxName, "sandbox_uid": input.SeedSubscription.SandboxUID,
			"identity_name": input.SeedSubscription.IdentityName, "identity_uid": input.SeedSubscription.IdentityUID,
		} {
			if strings.TrimSpace(value) == "" {
				t.Fatalf("seed_subscription field %q is required", name)
			}
		}
	}

	c := defaultConfig()
	c.DatabaseURL = input.DatabaseURL
	c.Namespace = input.Namespace
	c.TokenAudience = input.TokenAudience
	c.RPC = listener.Config{
		ListenAddress: "127.0.0.1:0",
		TLS:           &listener.TLS{CertFile: input.ServerCertFile, KeyFile: input.ServerKeyFile},
		Auth: []listener.Authentication{
			&listener.SPIFFEAuth{CAFile: input.ClientCAFile},
			&listener.KubernetesBearerAuth{Audiences: []string{input.TokenAudience}},
		},
	}
	c.Actions = actionPeer{URL: "http://127.0.0.1:1", TokenFile: input.PeerTokenFile, RequestTimeoutSeconds: 5}
	c.RunnerGateway = runnerGateway{ControlTarget: "127.0.0.1:1", TokenFile: input.PeerTokenFile, RequestTimeoutSeconds: 5}
	c.HistoryService = peer{Target: "127.0.0.1:1", TokenFile: input.PeerTokenFile, RequestTimeoutSeconds: 5}
	c.SessionDispatcher = peer{Target: "127.0.0.1:1", TokenFile: input.PeerTokenFile, RequestTimeoutSeconds: 5}
	if err := c.validate(); err != nil {
		t.Fatal(err)
	}

	var seeded *seededSubscription
	if input.SeedSubscription != nil {
		db := postgres.Open(t, input.DatabaseURL)
		requestID := uuid.New()
		idempotencyKey := "notification-process-" + uuid.NewString()
		created, err := store.New(db, c.Quotas).Subscribe(t.Context(), store.Owner{
			Identity:   model.Identity{Name: input.SeedSubscription.IdentityName, UID: input.SeedSubscription.IdentityUID},
			SandboxUID: input.SeedSubscription.SandboxUID,
		}, model.Subscribe{
			DestinationRef: model.DestinationRef{
				Namespace: input.Namespace, Name: input.SeedSubscription.SandboxName, UID: input.SeedSubscription.SandboxUID,
			},
			SessionID:      "notification-process-" + uuid.NewString(),
			IdempotencyKey: idempotencyKey,
			Source:         model.ActionsSource{RequestID: requestID},
			LifetimeDays:   7,
		}, nil)
		if err != nil {
			t.Fatal(err)
		}
		// Keep the real row listable without letting the worker call an absent Action Service.
		if _, err := db.ExecContext(t.Context(), `UPDATE subscription SET next_attempt = now() + interval '1 hour' WHERE id = $1`, created.ID); err != nil {
			t.Fatal(err)
		}
		seeded = &seededSubscription{ID: created.ID.String(), IdempotencyKey: created.IdempotencyKey, RequestID: requestID.String()}
	}

	deps, err := explicitKubeDeps(input.KubeconfigPath)
	if err != nil {
		t.Fatal(err)
	}
	grpcListener, err := net.Listen("tcp", c.RPC.ListenAddress)
	if err != nil {
		t.Fatal(err)
	}
	httpListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		_ = grpcListener.Close()
		t.Fatal(err)
	}
	ctx, stop := signalContext()
	defer stop()
	served := make(chan error, 1)
	go func() { served <- serve(ctx, c, deps, grpcListener, httpListener) }()
	if err := json.NewEncoder(os.Stdout).Encode(processReady{Address: grpcListener.Addr().String(), Subscription: seeded}); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-served:
		if err != nil && !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	case <-ctx.Done():
		if err := <-served; err != nil && !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	}
}

func explicitKubeDeps(path string) (kubeDeps, error) {
	kubeConfig, err := kubeclient.Config(path)
	if err != nil {
		return kubeDeps{}, err
	}
	kubeConfig.QPS = -1
	kube, err := kubernetes.NewForConfig(kubeConfig)
	if err != nil {
		return kubeDeps{}, err
	}
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{sandboxapi.AddToScheme, policyapi.AddToScheme} {
		if err := add(scheme); err != nil {
			return kubeDeps{}, err
		}
	}
	sandboxes, err := client.New(kubeConfig, client.Options{Scheme: scheme})
	if err != nil {
		return kubeDeps{}, err
	}
	return kubeDeps{Reviews: kube.AuthenticationV1().TokenReviews(), Sandboxes: sandboxes, Policies: kubeConfig}, nil
}

func writeKubeconfig(t *testing.T, config *rest.Config) string {
	t.Helper()
	read := func(name, path string, data []byte) []byte {
		t.Helper()
		if len(data) > 0 || path == "" {
			return data
		}
		contents, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("read Kubernetes %s: %v", name, err)
		}
		return contents
	}
	path := filepath.Join(t.TempDir(), "caller-envtest.kubeconfig")
	configFile := clientcmdapi.Config{
		CurrentContext: "caller-envtest",
		Clusters: map[string]*clientcmdapi.Cluster{
			"caller-envtest": {
				Server:                   config.Host,
				CertificateAuthorityData: read("CA", config.CAFile, config.CAData),
				TLSServerName:            config.ServerName,
				InsecureSkipTLSVerify:    config.Insecure,
			},
		},
		AuthInfos: map[string]*clientcmdapi.AuthInfo{
			"caller-envtest": {
				ClientCertificateData: read("client certificate", config.CertFile, config.CertData),
				ClientKeyData:         read("client key", config.KeyFile, config.KeyData),
				Token:                 config.BearerToken,
				Username:              config.Username,
				Password:              config.Password,
			},
		},
		Contexts: map[string]*clientcmdapi.Context{
			"caller-envtest": {Cluster: "caller-envtest", AuthInfo: "caller-envtest"},
		},
	}
	if err := clientcmd.WriteToFile(configFile, path); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

type childProcess struct {
	command      *exec.Cmd
	done         chan struct{}
	waitErr      error
	stderrPath   string
	address      string
	subscription *seededSubscription
	test         *testing.T
}

func startNotificationProcess(t *testing.T, configPath string) *childProcess {
	t.Helper()
	command := exec.Command(os.Args[0], "-test.run=^TestNotificationProcessDriver$")
	command.Env = append(os.Environ(), processConfigEnv+"="+configPath)
	stdout, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	outputsDir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if outputsDir == "" {
		outputsDir = t.TempDir()
	}
	stderrPath := filepath.Join(outputsDir, "notification-process-"+uuid.NewString()+".stderr")
	stderr, err := os.Create(stderrPath)
	if err != nil {
		t.Fatal(err)
	}
	command.Stderr = stderr
	if err := command.Start(); err != nil {
		_ = stderr.Close()
		t.Fatal(err)
	}
	done := make(chan struct{})
	process := &childProcess{command: command, done: done, stderrPath: stderrPath, test: t}
	t.Cleanup(process.stop)
	go func() {
		process.waitErr = command.Wait()
		_ = stderr.Close()
		close(done)
	}()
	line := make(chan struct {
		address      string
		subscription *seededSubscription
		err          error
	}, 1)
	go func() {
		contents, err := bufio.NewReader(stdout).ReadString('\n')
		if err != nil {
			line <- struct {
				address      string
				subscription *seededSubscription
				err          error
			}{err: err}
			return
		}
		var ready processReady
		err = json.Unmarshal([]byte(contents), &ready)
		if err == nil && ready.Address == "" {
			err = errors.New("Notification Service driver returned an empty gRPC address")
		}
		line <- struct {
			address      string
			subscription *seededSubscription
			err          error
		}{address: ready.Address, subscription: ready.Subscription, err: err}
	}()
	select {
	case ready := <-line:
		if ready.err != nil {
			select {
			case <-done:
				t.Fatalf("Notification Service driver exited before readiness (%v): %v; stderr artifact: %s", ready.err, process.waitErr, process.stderrPath)
			default:
				t.Fatalf("read Notification Service driver readiness: %v", ready.err)
			}
		}
		process.address = ready.address
		process.subscription = ready.subscription
	case <-done:
		t.Fatalf("Notification Service driver exited before readiness: %v; stderr artifact: %s", process.waitErr, process.stderrPath)
	case <-time.After(15 * time.Second):
		t.Fatal("Notification Service driver did not report its listener")
	}
	return process
}

func (p *childProcess) stop() {
	select {
	case <-p.done:
		if p.waitErr != nil {
			p.test.Errorf("Notification Service child exited with an error: %v; stderr artifact: %s", p.waitErr, p.stderrPath)
		}
		return
	default:
	}
	if err := p.command.Process.Signal(syscall.SIGTERM); err != nil && !errors.Is(err, os.ErrProcessDone) {
		p.test.Errorf("signal Notification Service child to stop: %v", err)
		_ = p.command.Process.Kill()
	}
	select {
	case <-p.done:
		if p.waitErr != nil {
			p.test.Errorf("Notification Service child exited with an error: %v; stderr artifact: %s", p.waitErr, p.stderrPath)
		}
	case <-time.After(10 * time.Second):
		_ = p.command.Process.Kill()
		<-p.done
		p.test.Error("Notification Service child did not stop after SIGTERM")
		if p.waitErr != nil {
			p.test.Errorf("Notification Service child exited after kill: %v; stderr artifact: %s", p.waitErr, p.stderrPath)
		}
	}
}

func signalContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}
