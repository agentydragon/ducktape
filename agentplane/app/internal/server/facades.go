package server

import (
	"context"
	"fmt"
	"time"

	actionpb "github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/web_push/web_push_servicepb"
	streamhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/streams"
	"github.com/agentydragon/ducktape/agentplane/app/internal/settings"
	upstreamproxy "github.com/agentydragon/ducktape/agentplane/app/internal/upstream"
	"github.com/agentydragon/ducktape/agentplane/egress/api/admin/egress_adminpb"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/api/connection/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
)

// configureFacades binds each browser route to its generated RPCs and its App credential.
func configureFacades(ctx context.Context, c settings.Config, streams *streamhttp.Dependencies) error {
	env := c.EnvironmentService
	for _, service := range []struct {
		name           string
		upstream       *streamhttp.Upstream
		target         string
		serviceURL     *string
		tokenFile      string
		timeoutSeconds float64
		options        facade.HandlerOptions
		registrations  []facade.Registration
	}{
		{
			name: "Action Service", upstream: &streams.ActionHTTP, serviceURL: &c.ActionFederation.ServiceURL,
			timeoutSeconds: 10,
			options:        facade.HandlerOptions{TLS: c.ActionsTLS},
			registrations: []facade.Registration{
				actionpb.RegisterActionServiceHandlerFromEndpoint,
				operator_action_servicepb.RegisterOperatorActionServiceHandlerFromEndpoint,
				web_push_servicepb.RegisterWebPushServiceHandlerFromEndpoint,
			},
		},
		{
			name: "Connection Service", upstream: &streams.ConnectionHTTP, serviceURL: &c.ConnectionServiceURL,
			timeoutSeconds: 10,
			options:        facade.HandlerOptions{TLS: c.ConnectionServiceTLS},
			registrations:  []facade.Registration{connection_servicepb.RegisterConnectionServiceHandlerFromEndpoint},
		},
		{
			name: "Session Dispatcher", upstream: &streams.DispatcherHTTP, target: c.SessionDispatcherTarget,
			tokenFile: c.SessionDispatcherTokenFile, timeoutSeconds: c.SessionDispatcherRequestTimeoutSecs,
			options:       facade.HandlerOptions{TLS: c.SessionDispatcherTLS},
			registrations: []facade.Registration{session_dispatcherpb.RegisterSessionDispatcherHandlerFromEndpoint},
		},
		{
			name: "Environment Service", upstream: &streams.EnvironmentHTTP, target: env.Target,
			timeoutSeconds: env.RequestTimeoutSeconds,
			options:        facade.HandlerOptions{TLS: env.TLS},
			registrations:  []facade.Registration{environmentpb.RegisterEnvironmentServiceHandlerFromEndpoint},
		},
		{
			name: "Policy Service", upstream: &streams.PolicyHTTP, target: c.PolicyServiceTarget,
			tokenFile: c.PolicyServiceTokenFile, timeoutSeconds: 10,
			options:       facade.HandlerOptions{TLS: c.PolicyServiceTLS},
			registrations: []facade.Registration{policypb.RegisterPolicyServiceHandlerFromEndpoint},
		},
		{
			name: "History Service", upstream: &streams.HistoryHTTP, target: c.HistoryServiceTarget,
			tokenFile: c.HistoryServiceTokenFile, timeoutSeconds: c.HistoryServiceRequestTimeoutSeconds,
			options:       facade.HandlerOptions{TLS: c.HistoryServiceTLS},
			registrations: []facade.Registration{historypb.RegisterHistoryServiceHandlerFromEndpoint},
		},
		{
			name: "Notification Service", upstream: &streams.NotificationHTTP, serviceURL: &c.NotificationsURL,
			tokenFile: c.NotificationsTokenFile, timeoutSeconds: c.NotificationsRequestTimeoutSeconds,
			options:       facade.HandlerOptions{TLS: c.NotificationsTLS, RejectUnknownJSONFields: true},
			registrations: []facade.Registration{notificationspb.RegisterNotificationsHandlerFromEndpoint},
		},
		{
			name: "Egress", upstream: &streams.EgressHTTP, serviceURL: &c.EgressAdminURL,
			tokenFile: c.EgressAdminTokenFile, timeoutSeconds: c.EgressAdminTimeoutSeconds,
			options:       facade.HandlerOptions{TLS: c.EgressAdminTLS},
			registrations: []facade.Registration{egress_adminpb.RegisterEgressAdminHandlerFromEndpoint},
		},
	} {
		target := service.target
		if service.serviceURL != nil {
			var err error
			target, err = grpcTargetFromHTTPURL(*service.serviceURL)
			if err != nil {
				return fmt.Errorf("configure %s gRPC client: %w", service.name, err)
			}
		}
		timeout := time.Duration(service.timeoutSeconds * float64(time.Second))
		handler, err := facade.NewHandlerWithOptions(ctx, target, timeout, service.options, service.registrations...)
		if err != nil {
			return fmt.Errorf("configure generated %s HTTP facade: %w", service.name, err)
		}
		*service.upstream = upstreamproxy.NewHandlerUpstream(handler, service.tokenFile)
	}
	return nil
}
