package api

import (
	"context"
	"errors"
	"maps"
	"slices"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/action_service/push"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/web_push_servicepb"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	// userAgentMetadata carries the User-Agent header of an HTTP facade request: gRPC-Gateway forwards the permanent
	// HTTP headers under its `grpcgateway-` prefix.
	userAgentMetadata = "grpcgateway-user-agent"
	// userAgentLimit is how many characters of the User-Agent a subscription keeps.
	userAgentLimit = 300
)

// errPushNotConfigured is a subscription offered while Web Push is not configured.
var errPushNotConfigured = errors.New("web push is not configured")

// PushService is the WebPushService server: the calling operator's own Web Push subscriptions.
type PushService struct {
	web_push_servicepb.UnimplementedWebPushServiceServer
	subscriptions *push.Subscriptions
	// identity is nil while Web Push is not configured.
	identity *push.Identity
	// updates wakes a watch on each commit to a subscription.
	updates *wakeups.Hub
	// gate admitted each call; a watch asks it again before every frame.
	gate *Gate
}

func NewPushService(subscriptions *push.Subscriptions, identity *push.Identity, updates *wakeups.Hub, gate *Gate) *PushService {
	return &PushService{subscriptions: subscriptions, identity: identity, updates: updates, gate: gate}
}

// pushOperator is the operator the call acts as: the Gate admits only operators to this service.
func pushOperator(ctx context.Context) store.OperatorPrincipal {
	return admitted(ctx).principal.(store.OperatorPrincipal)
}

func subscriptionView(subscription push.Subscription) *web_push_servicepb.PushSubscription {
	return &web_push_servicepb.PushSubscription{
		Endpoint: subscription.Endpoint, UserAgent: subscription.UserAgent, CreatedAt: timestamppb.New(subscription.CreatedAt),
	}
}

func (s *PushService) GetPushConfig(context.Context, *web_push_servicepb.GetPushConfigRequest) (*web_push_servicepb.GetPushConfigResponse, error) {
	response := &web_push_servicepb.GetPushConfigResponse{}
	if s.identity != nil {
		key := s.identity.ApplicationServerKey()
		response.ApplicationServerKey = &key
	}
	return response, nil
}

func (s *PushService) ListPushSubscriptions(ctx context.Context, _ *web_push_servicepb.ListPushSubscriptionsRequest) (*web_push_servicepb.ListPushSubscriptionsResponse, error) {
	subscriptions, err := s.subscriptions.List(ctx, pushOperator(ctx))
	if err != nil {
		return nil, err
	}
	response := &web_push_servicepb.ListPushSubscriptionsResponse{}
	for _, subscription := range subscriptions {
		response.PushSubscriptions = append(response.PushSubscriptions, subscriptionView(subscription))
	}
	return response, nil
}

// userAgent is the User-Agent of the HTTP request behind the call, cut to userAgentLimit characters; nil for a call
// that came over gRPC.
func userAgent(ctx context.Context) *string {
	md, _ := metadata.FromIncomingContext(ctx)
	values := md.Get(userAgentMetadata)
	if len(values) == 0 {
		return nil
	}
	agent := []rune(values[0])
	cut := string(agent[:min(len(agent), userAgentLimit)])
	return &cut
}

func (s *PushService) CreatePushSubscription(ctx context.Context, request *web_push_servicepb.CreatePushSubscriptionRequest) (*web_push_servicepb.CreatePushSubscriptionResponse, error) {
	for _, field := range []struct {
		name, value string
		most        int
	}{{"endpoint", request.GetEndpoint(), 2048}, {"p256dh", request.GetP256Dh(), 200}, {"auth", request.GetAuth(), 200}} {
		if count := utf8.RuneCountInString(field.value); count < 1 || count > field.most {
			return nil, invalidRequest("%s must be 1 to %d characters", field.name, field.most)
		}
	}
	if s.identity == nil {
		return nil, errPushNotConfigured
	}
	if err := s.identity.CheckEndpoint(request.GetEndpoint()); err != nil {
		return nil, err
	}
	if err := s.subscriptions.Save(ctx, pushOperator(ctx), push.Registration{
		Endpoint: request.GetEndpoint(), P256dh: request.GetP256Dh(), Auth: request.GetAuth(), UserAgent: userAgent(ctx),
	}); err != nil {
		return nil, err
	}
	return &web_push_servicepb.CreatePushSubscriptionResponse{}, nil
}

func (s *PushService) DeletePushSubscription(ctx context.Context, request *web_push_servicepb.DeletePushSubscriptionRequest) (*web_push_servicepb.DeletePushSubscriptionResponse, error) {
	if err := s.subscriptions.Delete(ctx, pushOperator(ctx), request.GetEndpoint()); err != nil {
		return nil, err
	}
	return &web_push_servicepb.DeletePushSubscriptionResponse{}, nil
}

// WatchPushSubscriptions sends the operator's subscriptions, then an upsert of each one added or changed, and the
// endpoint of each one removed. A change the operator cannot see, such as new keys or another operator's
// subscription, sends nothing.
func (s *PushService) WatchPushSubscriptions(_ *web_push_servicepb.WatchPushSubscriptionsRequest, stream grpc.ServerStreamingServer[web_push_servicepb.WatchPushSubscriptionsResponse]) error {
	ctx := stream.Context()
	writeCtx, stopWrites := watchWriteContext(ctx, s.gate)
	defer stopWrites()
	operator := pushOperator(ctx)
	updates, unsubscribe, err := s.updates.Subscribe(push.UpdatesChannel)
	if err != nil {
		return err
	}
	defer unsubscribe()
	// The watch reads after subscribing, so a commit in between is not missed.
	f := follower{gate: s.gate, updates: updates}
	sendFrame := func(frame *web_push_servicepb.WatchPushSubscriptionsResponse) error {
		return send(ctx, writeCtx, s.gate, stream, frame)
	}
	subscriptions, err := s.subscriptions.List(ctx, operator)
	if err != nil {
		return err
	}
	// shown is each subscription as the client holds it, by endpoint.
	shown := make(map[string]*web_push_servicepb.PushSubscription, len(subscriptions))
	for chunk := range slices.Chunk(subscriptions, snapshotChunk) {
		views := make([]*web_push_servicepb.PushSubscription, len(chunk))
		for i, subscription := range chunk {
			views[i] = subscriptionView(subscription)
			shown[subscription.Endpoint] = views[i]
		}
		if err := sendFrame(&web_push_servicepb.WatchPushSubscriptionsResponse{Frame: &web_push_servicepb.WatchPushSubscriptionsResponse_Snapshot{
			Snapshot: &web_push_servicepb.PushSubscriptionChunk{PushSubscriptions: views},
		}}); err != nil {
			return err
		}
	}
	if err := sendFrame(&web_push_servicepb.WatchPushSubscriptionsResponse{
		Frame: &web_push_servicepb.WatchPushSubscriptionsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}},
	}); err != nil {
		return err
	}
	for {
		if err := f.changed(ctx); err != nil {
			if errors.Is(err, errDraining) {
				// A planned renewal, as for WatchActionRequests.
				return send(ctx, ctx, s.gate, stream, &web_push_servicepb.WatchPushSubscriptionsResponse{
					Frame: &web_push_servicepb.WatchPushSubscriptionsResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}},
				})
			}
			return err
		}
		if subscriptions, err = s.subscriptions.List(ctx, operator); err != nil {
			return err
		}
		current := make(map[string]bool, len(subscriptions))
		for _, subscription := range subscriptions {
			current[subscription.Endpoint] = true
			view := subscriptionView(subscription)
			if proto.Equal(shown[subscription.Endpoint], view) {
				continue
			}
			if err := sendFrame(&web_push_servicepb.WatchPushSubscriptionsResponse{
				Frame: &web_push_servicepb.WatchPushSubscriptionsResponse_Upsert{Upsert: view},
			}); err != nil {
				return err
			}
			shown[subscription.Endpoint] = view
		}
		for _, endpoint := range slices.Sorted(maps.Keys(shown)) {
			if current[endpoint] {
				continue
			}
			if err := sendFrame(&web_push_servicepb.WatchPushSubscriptionsResponse{
				Frame: &web_push_servicepb.WatchPushSubscriptionsResponse_RemovedEndpoint{RemovedEndpoint: endpoint},
			}); err != nil {
				return err
			}
			delete(shown, endpoint)
		}
	}
}
