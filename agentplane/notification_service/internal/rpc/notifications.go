package rpc

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/inboxaccess"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
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
	sessions  Sessions
	// messageBodyLimit is in UTF-8 bytes.
	messageBodyLimit int
	timeout          time.Duration
	wakeups          *wakeups.Hub
	operatorReader   string
	debounce         store.Debounce
	draining         context.Context
	drain            context.CancelFunc
}

func NewNotifications(options Options) *Notifications {
	draining, drain := context.WithCancel(context.Background())
	return &Notifications{
		store: options.Store, namespace: options.Namespace, sandboxes: options.Sandboxes,
		runner: options.Runner, actions: options.Actions, github: options.GitHub, policy: options.SendPolicy,
		history: options.History, sessions: options.Sessions, messageBodyLimit: options.MessageBodyLimit, timeout: options.RequestTimeout,
		wakeups: options.Wakeups, operatorReader: options.OperatorReader, debounce: options.Debounce,
		draining: draining, drain: drain,
	}
}

// Drain ends ongoing watches before graceful server shutdown.
func (s *Notifications) Drain() { s.drain() }

// caller is the ServiceAccount workload Methods admitted, who it acts for.
func (s *Notifications) caller(ctx context.Context) (model.ServiceAccount, error) {
	principal, err := auth.Caller(ctx)
	if err != nil {
		return model.ServiceAccount{}, admissionRefused{err}
	}
	return model.ServiceAccount{Namespace: principal.Account.Namespace, Name: principal.Account.Name}, nil
}

// actor is an admitted workload as it acts on Sessions: an accountActor or an identityActor.
type actor interface{ isActor() }

// accountActor is a ServiceAccount incarnation.
type accountActor struct {
	account model.ServiceAccount
	uid     string
}

// identityActor is an Identity incarnation, running in the Sandbox incarnation that controls its
// workload credential.
type identityActor struct {
	identity model.Identity
	sandbox  model.DestinationRef
}

func (accountActor) isActor()  {}
func (identityActor) isActor() {}

// admittedActor is the workload Methods admitted. An Identity caller whose credential no Sandbox
// controls acts for no Sandbox and is refused with errSandboxMissing.
func (s *Notifications) admittedActor(ctx context.Context) (actor, error) {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return nil, admissionRefused{err}
	}
	if identity, ok := admission.(auth.IdentityAdmission); ok {
		owner, ok := identity.Caller.Workload.Owner.(auth.ControllerOwner)
		if !ok || owner.APIVersion != sandboxapi.GroupVersion.String() || owner.Kind != sandboxapi.SandboxKind {
			return nil, errSandboxMissing
		}
		return identityActor{
			identity: model.Identity{Name: identity.Caller.Identity.Name, UID: identity.Caller.Identity.UID},
			sandbox:  model.DestinationRef{Namespace: identity.Caller.Workload.Namespace, Name: owner.Name, UID: owner.UID},
		}, nil
	}
	principal, err := auth.Caller(ctx)
	if err != nil {
		return nil, admissionRefused{err}
	}
	return accountActor{
		account: model.ServiceAccount{Namespace: principal.Account.Namespace, Name: principal.Account.Name},
		uid:     principal.ServiceAccountUID,
	}, nil
}

// serve runs handle for the admitted workload under the request timeout.
func serve[Request, Response any](
	ctx context.Context, s *Notifications, request *Request,
	handle func(context.Context, actor, *Request) (*Response, error),
) (*Response, error) {
	ctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	caller, err := s.admittedActor(ctx)
	if err != nil {
		return nil, failure(ctx, err)
	}
	response, err := handle(ctx, caller, request)
	if err != nil {
		return nil, failure(ctx, err)
	}
	return response, nil
}

// serveOwned runs handle under the request timeout for a request about the caller's inboxes and subscriptions:
// those opened for the Sandbox incarnation it runs in now, owned by its ServiceAccount or Identity incarnation
// (currentWorkload). A Sandbox recreated under a deleted one's name sees none of the deleted one's. A caller whose
// Sandbox is gone gets none, or NOT_FOUND if none is nil.
func serveOwned[Request, Response any](
	ctx context.Context, s *Notifications, request *Request, none *Response,
	handle func(context.Context, store.Owner, *Request) (*Response, error),
) (*Response, error) {
	ctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	caller, err := s.currentWorkload(ctx)
	if errors.Is(err, errSandboxMissing) && none != nil {
		return none, nil
	}
	if err != nil {
		return nil, failure(ctx, err)
	}
	response, err := handle(ctx, caller.owner, request)
	if err != nil {
		return nil, failure(ctx, err)
	}
	return response, nil
}

func (s *Notifications) CreateSubscription(ctx context.Context, request *notificationspb.CreateSubscriptionRequest) (*notificationspb.CreateSubscriptionResponse, error) {
	return serve(ctx, s, request, s.createSubscription)
}

func (s *Notifications) createSubscription(ctx context.Context, owner actor, request *notificationspb.CreateSubscriptionRequest) (*notificationspb.CreateSubscriptionResponse, error) {
	body, err := subscribeBody(request)
	if err != nil {
		return nil, err
	}
	if err := model.CheckText("session_id", body.SessionID, 1, 200); err != nil {
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
	ctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	operator, err := s.operator(ctx)
	if err != nil {
		return nil, failure(ctx, err)
	}
	if !operator {
		return serveOwned(ctx, s, request, nil, s.readInbox)
	}
	if err := rejectRetiredInboxAddress(request); err != nil {
		return nil, failure(ctx, err)
	}
	if err := model.CheckText("session_id", request.GetSessionId(), 1, 200); err != nil {
		return nil, failure(ctx, err)
	}
	page, err := s.store.ReadSession(ctx, s.namespace, request.GetSessionId(), request.GetAfterCursor(), readLimit(request.Limit))
	if err != nil {
		return nil, failure(ctx, err)
	}
	response, err := readInboxResponse(page)
	if err != nil {
		return nil, failure(ctx, err)
	}
	return response, nil
}

func (s *Notifications) readInbox(ctx context.Context, owner store.Owner, request *notificationspb.ReadInboxRequest) (*notificationspb.ReadInboxResponse, error) {
	id, err := s.ownedInboxID(ctx, owner, request, request.GetSessionId())
	if err != nil {
		return nil, err
	}
	page, err := s.store.Read(ctx, owner, id, request.GetAfterCursor(), readLimit(request.Limit))
	if err != nil {
		return nil, err
	}
	return readInboxResponse(page)
}

func readInboxResponse(page store.InboxPage) (*notificationspb.ReadInboxResponse, error) {
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
	id, err := s.ownedInboxID(ctx, owner, request, request.GetSessionId())
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
	id, err := s.ownedInboxID(ctx, owner, request, request.GetSessionId())
	if err != nil {
		return nil, err
	}
	return &notificationspb.RetireInboxResponse{}, s.store.Retire(ctx, owner, id)
}

// ownedInboxID rejects retired addresses before resolving the Session for its current owner.
func (s *Notifications) ownedInboxID(ctx context.Context, owner store.Owner, request proto.Message, sessionID string) (uuid.UUID, error) {
	if err := rejectRetiredInboxAddress(request); err != nil {
		return uuid.Nil, err
	}
	if err := model.CheckText("session_id", sessionID, 1, 200); err != nil {
		return uuid.Nil, err
	}
	return s.store.InboxIDForSession(ctx, owner, sessionID)
}

func rejectRetiredInboxAddress(request proto.Message) error {
	for unknown := request.ProtoReflect().GetUnknown(); len(unknown) > 0; {
		field, _, size := protowire.ConsumeField(unknown)
		if size < 0 {
			return invalidRequest("invalid inbox request")
		}
		if field == 1 {
			return invalidRequest("inbox_id is no longer accepted; address the Session by ID")
		}
		unknown = unknown[size:]
	}
	return nil
}
