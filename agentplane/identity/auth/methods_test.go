package auth_test

import (
	"context"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
)

// callerHealth stands in for a served API: it answers SERVING and records the caller each call was admitted as.
type callerHealth struct {
	healthpb.UnimplementedHealthServer
	callers chan auth.Principal
}

func (h callerHealth) record(ctx context.Context) {
	principal, err := auth.Caller(ctx)
	if err == nil {
		h.callers <- principal
	}
}

func (h callerHealth) Check(ctx context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	h.record(ctx)
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}

func (h callerHealth) Watch(_ *healthpb.HealthCheckRequest, stream grpc.ServerStreamingServer[healthpb.HealthCheckResponse]) error {
	h.record(stream.Context())
	return stream.Send(&healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING})
}

// serveHealth serves callerHealth behind methods and returns a client and the callers the calls were admitted as.
func serveHealth(t *testing.T, methods auth.Methods, admissionTimeout time.Duration) (healthpb.HealthClient, <-chan auth.Principal) {
	t.Helper()
	callers := make(chan auth.Principal, 8)
	server := grpc.NewServer(methods.ServerOptions(admissionTimeout)...)
	healthpb.RegisterHealthServer(server, callerHealth{callers: callers})
	address := grpcserver.Serve(t, server)
	connection, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return healthpb.NewHealthClient(connection), callers
}

func bearing(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	t.Cleanup(cancel)
	if token == "" {
		return ctx
	}
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
}

// checkAndWatch is the codes of a unary Check and of a Watch's first frame.
func checkAndWatch(ctx context.Context, client healthpb.HealthClient) (codes.Code, codes.Code) {
	_, checked := client.Check(ctx, &healthpb.HealthCheckRequest{})
	stream, err := client.Watch(ctx, &healthpb.HealthCheckRequest{})
	if err == nil {
		_, err = stream.Recv()
	}
	return status.Code(checked), status.Code(err)
}

func both(t *testing.T, ctx context.Context, client healthpb.HealthClient, want codes.Code) {
	t.Helper()
	if check, watch := checkAndWatch(ctx, client); check != want || watch != want {
		t.Errorf("Check %v, Watch %v; want %v", check, watch, want)
	}
}

func TestAMethodNoRuleListsIsRefusedBeforeResolvingItsSubject(t *testing.T) {
	resolver := new(unusedSubjectResolver)
	for name, methods := range map[string]auth.Methods{
		"unlisted":      {healthpb.Health_List_FullMethodName: auth.AnySubject(resolver)},
		"the zero Rule": {healthpb.Health_Check_FullMethodName: {}, healthpb.Health_Watch_FullMethodName: {}},
	} {
		t.Run(name, func(t *testing.T) {
			client, _ := serveHealth(t, methods, 0)
			both(t, bearing(t, ""), client, codes.PermissionDenied)
		})
	}
	if calls := resolver.calls.Load(); calls != 0 {
		t.Fatalf("%d subject lookups for methods no Rule admits to", calls)
	}
}

func TestHealthMethodsNeedNoBearer(t *testing.T) {
	client, callers := serveHealth(t, auth.HealthMethods(), 0)
	both(t, bearing(t, ""), client, codes.OK)
	select {
	case caller := <-callers:
		t.Fatalf("a public call carried caller %+v", caller)
	default:
	}
}

type waitingSubjectResolver struct{}

func (waitingSubjectResolver) ResolveSubject(ctx context.Context) (auth.Subject, error) {
	<-ctx.Done()
	return nil, status.FromContextError(ctx.Err()).Err()
}

func TestTheAdmissionTimeoutBoundsSubjectResolution(t *testing.T) {
	rule := auth.AnySubject(waitingSubjectResolver{})
	client, _ := serveHealth(t, auth.Methods{
		healthpb.Health_Check_FullMethodName: rule, healthpb.Health_Watch_FullMethodName: rule,
	}, 50*time.Millisecond)
	both(t, bearing(t, ""), client, codes.DeadlineExceeded)
}

func TestCallerRefusesACallWithoutAccountProof(t *testing.T) {
	if _, err := auth.Caller(context.Background()); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("Caller without an admission: %v", err)
	}
}

// legacyServiceAccountEnvelope encodes the former field-1 effective ServiceAccount caller using
// protobuf wire primitives. Current generated code reserves and ignores that field, so admission
// must reject it as an envelope with no effective Identity.
func legacyServiceAccountEnvelope() []byte {
	appendBytes := func(target []byte, number protowire.Number, value []byte) []byte {
		target = protowire.AppendTag(target, number, protowire.BytesType)
		return protowire.AppendBytes(target, value)
	}
	account := appendBytes(nil, 1, []byte(testNamespace))
	account = appendBytes(account, 2, []byte("test-caller"))
	serviceAccount := appendBytes(nil, 1, account)
	serviceAccount = appendBytes(serviceAccount, 2, []byte("old-service-account-uid"))
	delegation := appendBytes(nil, 1, serviceAccount)
	action := appendBytes(nil, 1, []byte("11111111-1111-4111-8111-111111111111"))
	action = appendBytes(action, 2, []byte("22222222-2222-4222-8222-222222222222"))
	return appendBytes(delegation, 5, action)
}

type identityUIDReaderFunc func(context.Context, string) (string, bool, error)

func (read identityUIDReaderFunc) GetIdentityUID(ctx context.Context, name string) (string, bool, error) {
	return read(ctx, name)
}
