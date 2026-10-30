package api

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/inboxaccess"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
)

// Notifications is the workloads' API: subscriptions that deliver events from a source into a Session's inbox,
// messages one workload sends to another's Session, and the inbox an agent reads and acknowledges.
type Notifications struct {
	notificationspb.UnimplementedNotificationsServer
	store     *store.Store
	namespace string
	sandboxes worker.Sandboxes
	runner    Runner
	actions   Actions
	github    GitHub
	policy    *inboxaccess.Policy
	history   SessionHistory
	// messageBodyLimit is in UTF-8 bytes.
	messageBodyLimit int
	timeout          time.Duration
}

func NewNotifications(options Options) *Notifications {
	return &Notifications{
		store: options.Store, namespace: options.Namespace, sandboxes: options.Sandboxes,
		runner: options.Runner, actions: options.Actions, github: options.GitHub, policy: options.SendPolicy,
		history: options.History, messageBodyLimit: options.MessageBodyLimit, timeout: options.RequestTimeout,
	}
}

// caller is the workload Methods admitted. Its ServiceAccount is who it acts for.
func (s *Notifications) caller(ctx context.Context) (model.ServiceAccount, error) {
	principal, err := auth.Caller(ctx)
	if err != nil {
		return model.ServiceAccount{}, admissionRefused{err}
	}
	return model.ServiceAccount{Namespace: principal.Account.Namespace, Name: principal.Account.Name}, nil
}

// serve runs handle for the admitted account under the request timeout.
func serve[Request, Response any](
	ctx context.Context, s *Notifications, request *Request,
	handle func(context.Context, model.ServiceAccount, *Request) (*Response, error),
) (*Response, error) {
	ctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	owner, err := s.caller(ctx)
	if err != nil {
		return nil, failure(ctx, err)
	}
	response, err := handle(ctx, owner, request)
	if err != nil {
		return nil, failure(ctx, err)
	}
	return response, nil
}

// serveOwned is serve for a request about the caller's inboxes and subscriptions: those opened for the Sandbox that
// runs as its account now. A Sandbox runs as the ServiceAccount of its own name, so one recreated under a deleted
// Sandbox's name sees none of the deleted one's. An account no Sandbox runs as gets none, or NOT_FOUND if none is nil.
func serveOwned[Request, Response any](
	ctx context.Context, s *Notifications, request *Request, none *Response,
	handle func(context.Context, store.Owner, *Request) (*Response, error),
) (*Response, error) {
	return serve(ctx, s, request, func(ctx context.Context, account model.ServiceAccount, request *Request) (*Response, error) {
		sandbox, err := s.sandbox(ctx, model.DestinationRef{Namespace: account.Namespace, Name: account.Name})
		if errors.Is(err, errSandboxMissing) && none != nil {
			return none, nil
		}
		if err != nil {
			return nil, err
		}
		return handle(ctx, store.Owner{Account: account, SandboxUID: sandbox.Identity.UID}, request)
	})
}

func (s *Notifications) CreateSubscription(ctx context.Context, request *notificationspb.CreateSubscriptionRequest) (*notificationspb.CreateSubscriptionResponse, error) {
	return serve(ctx, s, request, s.createSubscription)
}

func (s *Notifications) createSubscription(ctx context.Context, owner model.ServiceAccount, request *notificationspb.CreateSubscriptionRequest) (*notificationspb.CreateSubscriptionResponse, error) {
	body, err := subscribeBody(request)
	if err != nil {
		return nil, err
	}
	if err := body.Validate(); err != nil {
		return nil, err
	}
	subscription, err := s.subscribe(ctx, owner, body)
	if err != nil {
		return nil, err
	}
	view, err := subscriptionView(subscription)
	return &notificationspb.CreateSubscriptionResponse{Subscription: view}, err
}

func (s *Notifications) ListSubscriptions(ctx context.Context, request *notificationspb.ListSubscriptionsRequest) (*notificationspb.ListSubscriptionsResponse, error) {
	return serveOwned(ctx, s, request, &notificationspb.ListSubscriptionsResponse{}, s.listSubscriptions)
}

func (s *Notifications) listSubscriptions(ctx context.Context, owner store.Owner, request *notificationspb.ListSubscriptionsRequest) (*notificationspb.ListSubscriptionsResponse, error) {
	after, err := parseOptionalID("after_id", request.AfterId)
	if err != nil {
		return nil, err
	}
	subscriptions, err := s.store.ListSubscriptions(ctx, owner, after)
	if err != nil {
		return nil, err
	}
	response := &notificationspb.ListSubscriptionsResponse{}
	for _, subscription := range subscriptions {
		view, err := subscriptionView(subscription)
		if err != nil {
			return nil, err
		}
		response.Subscriptions = append(response.Subscriptions, view)
	}
	return response, nil
}

func (s *Notifications) GetSubscription(ctx context.Context, request *notificationspb.GetSubscriptionRequest) (*notificationspb.GetSubscriptionResponse, error) {
	return serveOwned(ctx, s, request, nil, s.getSubscription)
}

func (s *Notifications) getSubscription(ctx context.Context, owner store.Owner, request *notificationspb.GetSubscriptionRequest) (*notificationspb.GetSubscriptionResponse, error) {
	id, err := parseID("subscription_id", request.GetSubscriptionId())
	if err != nil {
		return nil, err
	}
	subscription, err := s.store.GetSubscription(ctx, owner, id)
	if err != nil {
		return nil, err
	}
	view, err := subscriptionView(subscription)
	return &notificationspb.GetSubscriptionResponse{Subscription: view}, err
}

func (s *Notifications) RenewSubscription(ctx context.Context, request *notificationspb.RenewSubscriptionRequest) (*notificationspb.RenewSubscriptionResponse, error) {
	return serveOwned(ctx, s, request, nil, s.renewSubscription)
}

func (s *Notifications) renewSubscription(ctx context.Context, owner store.Owner, request *notificationspb.RenewSubscriptionRequest) (*notificationspb.RenewSubscriptionResponse, error) {
	id, err := parseID("subscription_id", request.GetSubscriptionId())
	if err != nil {
		return nil, err
	}
	subscription, err := s.store.Renew(ctx, owner, id, request.GetVersion(), lifetimeDays(request.LifetimeDays))
	if err != nil {
		return nil, err
	}
	view, err := subscriptionView(subscription)
	return &notificationspb.RenewSubscriptionResponse{Subscription: view}, err
}

func (s *Notifications) CancelSubscription(ctx context.Context, request *notificationspb.CancelSubscriptionRequest) (*notificationspb.CancelSubscriptionResponse, error) {
	return serveOwned(ctx, s, request, nil, s.cancelSubscription)
}

func (s *Notifications) cancelSubscription(ctx context.Context, owner store.Owner, request *notificationspb.CancelSubscriptionRequest) (*notificationspb.CancelSubscriptionResponse, error) {
	id, err := parseID("subscription_id", request.GetSubscriptionId())
	if err != nil {
		return nil, err
	}
	subscription, err := s.store.Cancel(ctx, owner, id)
	if err != nil {
		return nil, err
	}
	view, err := subscriptionView(subscription)
	return &notificationspb.CancelSubscriptionResponse{Subscription: view}, err
}

func (s *Notifications) ListInboxes(ctx context.Context, request *notificationspb.ListInboxesRequest) (*notificationspb.ListInboxesResponse, error) {
	return serveOwned(ctx, s, request, &notificationspb.ListInboxesResponse{}, s.listInboxes)
}

func (s *Notifications) listInboxes(ctx context.Context, owner store.Owner, _ *notificationspb.ListInboxesRequest) (*notificationspb.ListInboxesResponse, error) {
	inboxes, err := s.store.ListInboxes(ctx, owner)
	if err != nil {
		return nil, err
	}
	response := &notificationspb.ListInboxesResponse{}
	for _, inbox := range inboxes {
		response.Inboxes = append(response.Inboxes, inboxView(inbox))
	}
	return response, nil
}

func (s *Notifications) ReadInbox(ctx context.Context, request *notificationspb.ReadInboxRequest) (*notificationspb.ReadInboxResponse, error) {
	return serveOwned(ctx, s, request, nil, s.readInbox)
}

func (s *Notifications) readInbox(ctx context.Context, owner store.Owner, request *notificationspb.ReadInboxRequest) (*notificationspb.ReadInboxResponse, error) {
	id, err := parseID("inbox_id", request.GetInboxId())
	if err != nil {
		return nil, err
	}
	page, err := s.store.Read(ctx, owner, id, request.GetAfterCursor(), readLimit(request.Limit))
	if err != nil {
		return nil, err
	}
	response := &notificationspb.ReadInboxResponse{Inbox: inboxView(page.Inbox), Notice: noticeView(page.Notice)}
	for _, entry := range page.Entries {
		view, err := entryView(entry)
		if err != nil {
			return nil, err
		}
		response.Entries = append(response.Entries, view)
	}
	return response, nil
}

func (s *Notifications) AcknowledgeInbox(ctx context.Context, request *notificationspb.AcknowledgeInboxRequest) (*notificationspb.AcknowledgeInboxResponse, error) {
	return serveOwned(ctx, s, request, nil, s.acknowledgeInbox)
}

func (s *Notifications) acknowledgeInbox(ctx context.Context, owner store.Owner, request *notificationspb.AcknowledgeInboxRequest) (*notificationspb.AcknowledgeInboxResponse, error) {
	id, err := parseID("inbox_id", request.GetInboxId())
	if err != nil {
		return nil, err
	}
	inbox, err := s.store.Acknowledge(ctx, owner, id, request.GetThroughCursor())
	if err != nil {
		return nil, err
	}
	return &notificationspb.AcknowledgeInboxResponse{Inbox: inboxView(inbox)}, nil
}

func (s *Notifications) RetireInbox(ctx context.Context, request *notificationspb.RetireInboxRequest) (*notificationspb.RetireInboxResponse, error) {
	return serveOwned(ctx, s, request, nil, s.retireInbox)
}

func (s *Notifications) retireInbox(ctx context.Context, owner store.Owner, request *notificationspb.RetireInboxRequest) (*notificationspb.RetireInboxResponse, error) {
	id, err := parseID("inbox_id", request.GetInboxId())
	if err != nil {
		return nil, err
	}
	return &notificationspb.RetireInboxResponse{}, s.store.Retire(ctx, owner, id)
}
