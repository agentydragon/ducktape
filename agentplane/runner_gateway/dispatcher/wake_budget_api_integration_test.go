package dispatcher

import (
	"context"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	wakebudgetpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/wakebudget/wakebudgetpb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestWakeBudgetRPCRequiresConfiguredPodBoundController(t *testing.T) {
	controller := auth.Account{Namespace: "budget-test", Name: "controller"}
	other := auth.Account{Namespace: "budget-test", Name: "sender"}
	resolver, err := auth.NewResolver(tokenReviewStub{
		"controller-pod":          {account: controller, uid: "controller-uid", pod: true},
		"controller-account-only": {account: controller, uid: "controller-uid"},
		"sender-pod":              {account: other, uid: "sender-uid", pod: true},
	}, authorityTestAudience, []string{controller.Namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	store := testStore(t, 3)
	service := &WakeBudgetService{Store: store, ControllerAccount: &controller}
	connection := grpcmethods.Serve(t, WakeBudgetMethods(resolver), func(server *grpc.Server) {
		wakebudgetpb.RegisterWakeBudgetServer(server, service)
	})
	client := wakebudgetpb.NewWakeBudgetClient(connection)
	request := &wakebudgetpb.ReserveWakeAttemptRequest{
		CallerNamespace: "budget-test", CallerName: "effective", CallerUid: "effective-uid",
		SandboxUid: "00000000-0000-4000-8000-000000000095", WakeGeneration: 1,
	}
	call := func(token string) (*wakebudgetpb.ReserveWakeAttemptResponse, error) {
		ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
		defer cancel()
		return client.ReserveWakeAttempt(metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token), request)
	}
	for _, test := range []struct {
		name, token string
		want        codes.Code
	}{
		{"no bearer", "", codes.Unauthenticated},
		{"wrong account", "sender-pod", codes.PermissionDenied},
		{"account-only token", "controller-account-only", codes.PermissionDenied},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, err := call(test.token)
			if status.Code(err) != test.want {
				t.Fatalf("reservation status = %v, want %v", err, test.want)
			}
		})
	}
	service.ControllerAccount = nil
	if _, err := call("controller-pod"); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("unconfigured controller reservation = %v", err)
	}
	service.ControllerAccount = &controller
	ctx := metadata.AppendToOutgoingContext(t.Context(), "authorization", "Agentplane-Delegated controller-pod")
	if _, err := client.ReserveWakeAttempt(ctx, request); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("delegated reservation = %v", err)
	}
	if response, err := call("controller-pod"); err != nil || response.GetRetryAfter().AsDuration() != 0 {
		t.Fatalf("valid controller reservation = %v, %v", response, err)
	}
	for _, invalid := range []*wakebudgetpb.ReserveWakeAttemptRequest{
		{CallerNamespace: "budget-test", CallerName: "effective", SandboxUid: "00000000-0000-4000-8000-000000000095", WakeGeneration: 2},
		{CallerNamespace: "budget-test", CallerName: "effective", CallerUid: "effective-uid", WakeGeneration: 2},
		{CallerNamespace: "budget-test", CallerName: "effective", CallerUid: "effective-uid", SandboxUid: "00000000-0000-4000-8000-000000000095"},
	} {
		ctx := metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer controller-pod")
		if _, err := client.ReserveWakeAttempt(ctx, invalid); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("invalid request accepted: %v", err)
		}
	}
	service.Store = nil
	if _, err := call("controller-pod"); status.Code(err) != codes.Unavailable {
		t.Fatalf("unavailable storage reservation = %v", err)
	}
	service.Store = store
	if err := store.DB.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := call("controller-pod"); status.Code(err) != codes.Unavailable {
		t.Fatalf("closed database reservation = %v", err)
	}
}

func TestWakeBudgetRPCSharesStoreLimitAndDuplicateGeneration(t *testing.T) {
	controller := auth.Account{Namespace: "budget-test", Name: "controller"}
	resolver, err := auth.NewResolver(tokenReviewStub{
		"controller-pod": {account: controller, uid: "controller-uid", pod: true},
	}, authorityTestAudience, []string{controller.Namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	store := testStore(t, 3)
	connection := grpcmethods.Serve(t, WakeBudgetMethods(resolver), func(server *grpc.Server) {
		wakebudgetpb.RegisterWakeBudgetServer(server, &WakeBudgetService{Store: store, ControllerAccount: &controller})
	})
	client := wakebudgetpb.NewWakeBudgetClient(connection)
	ctx := metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer controller-pod")
	key := lifecycle.WakeBudgetKey{Namespace: "budget-test", Name: "effective", UID: "effective-uid"}
	request := &wakebudgetpb.ReserveWakeAttemptRequest{
		CallerNamespace: key.Namespace, CallerName: key.Name, CallerUid: key.UID,
		SandboxUid: "00000000-0000-4000-8000-000000000096",
	}
	for generation := int64(1); generation <= WakeLimit; generation++ {
		request.WakeGeneration = generation
		if generation%2 == 0 {
			if wait, err := store.ReserveWakeAttempt(ctx, key, request.SandboxUid, generation); err != nil || wait != 0 {
				t.Fatalf("direct reservation %d = %s, %v", generation, wait, err)
			}
		} else if response, err := client.ReserveWakeAttempt(ctx, request); err != nil || response.GetRetryAfter().AsDuration() != 0 {
			t.Fatalf("RPC reservation %d = %v, %v", generation, response, err)
		}
	}
	request.WakeGeneration = WakeLimit + 1
	if response, err := client.ReserveWakeAttempt(ctx, request); err != nil || response.GetRetryAfter().AsDuration() <= 0 {
		t.Fatalf("exhausted budget = %v, %v", response, err)
	}
	request.WakeGeneration = 1
	if response, err := client.ReserveWakeAttempt(ctx, request); err != nil || response.GetRetryAfter().AsDuration() != 0 {
		t.Fatalf("duplicate generation = %v, %v", response, err)
	}
	request.CallerUid = "replacement-uid"
	if response, err := client.ReserveWakeAttempt(ctx, request); err != nil || response.GetRetryAfter().AsDuration() != 0 {
		t.Fatalf("replacement caller inherited budget = %v, %v", response, err)
	}
}
