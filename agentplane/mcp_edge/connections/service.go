package connections

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Gate admits the service's calls before they run (agentplane/docs/grpc_services.md): only an operator's, and none
// once the service drains.
type Gate interface {
	// Operator is the issuer and subject of the operator the call was admitted as.
	Operator(ctx context.Context) (issuer, subject string)
	// Recheck verifies the call's bearer again. It fails, with the status the call ends with, once the bearer no
	// longer proves the operator the call was admitted as.
	Recheck(ctx context.Context) error
	// DrainingContext is cancelled once the service stops accepting work.
	DrainingContext() context.Context
}

// Service is the ConnectionService server.
type Service struct {
	pb.UnimplementedConnectionServiceServer
	store *Store
	gate  Gate
	// updates wakes a watch on each commit to a Connection or a grant; it must listen to UpdatesChannel.
	updates *wakeups.Hub
}

func NewService(store *Store, gate Gate, updates *wakeups.Hub) *Service {
	return &Service{store: store, gate: gate, updates: updates}
}

// failure answers a failed call with the status code connection_service.proto names for it. A refusal this package
// wrote is answered as it is; a fault of the database gets a fixed message, and its cause is logged.
func failure(ctx context.Context, err error) error {
	switch {
	case err == nil, status.Code(err) != codes.Unknown:
		return err
	case errors.Is(err, errStored):
		slog.ErrorContext(ctx, "stored Connection cannot be shown", "error", err)
		return status.Error(codes.Internal, errStored.Error())
	case ctx.Err() != nil:
		return status.FromContextError(ctx.Err()).Err()
	default:
		slog.WarnContext(ctx, "Connection call failed", "error", err)
		return status.Error(codes.Unavailable, "the Connection service is temporarily unavailable")
	}
}

func invalid(format string, args ...any) error {
	return status.Error(codes.InvalidArgument, fmt.Sprintf(format, args...))
}

func (s *Service) operator(ctx context.Context) Operator {
	issuer, subject := s.gate.Operator(ctx)
	return Operator{Issuer: issuer, Subject: subject}
}

func parseConnectionID(value string) (uuid.UUID, error) {
	id, err := uuid.Parse(value)
	if err != nil {
		return uuid.Nil, invalid("connection_id must be a UUID")
	}
	return id, nil
}

// checkVersion refuses an expected version no Connection or enrollment can have: they start at 1.
func checkVersion(field string, version int64) error {
	if version < 1 {
		return invalid("%s must be at least 1", field)
	}
	return nil
}

// displayName is the name without surrounding whitespace, which must be 1 to 200 characters.
func displayName(name string) (string, error) {
	trimmed := strings.TrimSpace(name)
	if count := utf8.RuneCountInString(trimmed); count < 1 || count > 200 {
		return "", invalid("display_name must be 1 to 200 characters after trimming whitespace")
	}
	return trimmed, nil
}

func parseAccount(field string, account *sandboxes.ServiceAccount) (auth.Account, error) {
	if account.GetNamespace() == "" || account.GetName() == "" {
		return auth.Account{}, invalid("%s must set both namespace and name", field)
	}
	return auth.Account{Namespace: account.GetNamespace(), Name: account.GetName()}, nil
}

func serviceAccount(account auth.Account) *sandboxes.ServiceAccount {
	return &sandboxes.ServiceAccount{Namespace: account.Namespace, Name: account.Name}
}

func timestamp(t *time.Time) *timestamppb.Timestamp {
	if t == nil {
		return nil
	}
	return timestamppb.New(*t)
}

var grantStatuses = map[grantStatus]pb.GrantStatus{
	grantPending: pb.GrantStatus_GRANT_STATUS_PENDING,
	grantActive:  pb.GrantStatus_GRANT_STATUS_ACTIVE,
	grantRevoked: pb.GrantStatus_GRANT_STATUS_REVOKED,
}

var verdicts = map[verdict]action_servicepb.Verdict{
	verdictAllow: action_servicepb.Verdict_VERDICT_ALLOW,
	verdictDeny:  action_servicepb.Verdict_VERDICT_DENY,
}

func connectionView(c connection) (*pb.Connection, error) {
	view := &pb.Connection{
		Id: c.id.String(), DisplayName: c.displayName, Version: c.version,
		CreatedAt: timestamppb.New(c.createdAt), UpdatedAt: timestamppb.New(c.updatedAt),
	}
	if c.boundCaller != nil {
		view.BoundCaller = serviceAccount(*c.boundCaller)
	}
	for _, g := range c.grants {
		grantStatus, known := grantStatuses[g.status]
		if !known {
			return nil, fmt.Errorf("%w: grant %s has status %q", errStored, g.id, g.status)
		}
		view.Grants = append(view.Grants, &pb.Grant{
			Id: g.id.String(), ConnectionId: g.connectionID.String(), Revision: g.revision,
			Caller: serviceAccount(g.caller), Issuer: g.issuer, ClientId: g.clientID, Status: grantStatus,
			CreatedAt:   timestamppb.New(g.createdAt),
			ActivatedAt: timestamp(g.activatedAt), RevokedAt: timestamp(g.revokedAt),
		})
	}
	return view, nil
}

func connectionViews(connections []connection) ([]*pb.Connection, error) {
	views := make([]*pb.Connection, len(connections))
	for i, c := range connections {
		var err error
		if views[i], err = connectionView(c); err != nil {
			return nil, err
		}
	}
	return views, nil
}

// ListCallerServiceAccounts answers from the view of the callers, which must be current: a picker of accounts the
// view cannot vouch for would offer ones every consent then refuses.
func (s *Service) ListCallerServiceAccounts(ctx context.Context, _ *pb.ListCallerServiceAccountsRequest) (*pb.ListCallerServiceAccountsResponse, error) {
	accounts, current := s.store.callers.CallerServiceAccounts()
	if !current {
		return nil, status.Error(codes.Unavailable, "the view of the caller ServiceAccounts is not current")
	}
	response := &pb.ListCallerServiceAccountsResponse{}
	for _, account := range accounts {
		response.ServiceAccounts = append(response.ServiceAccounts, serviceAccount(account))
	}
	return response, nil
}

func (s *Service) ListConnections(ctx context.Context, _ *pb.ListConnectionsRequest) (*pb.ListConnectionsResponse, error) {
	connections, err := s.store.list(ctx, nil, 0)
	if err != nil {
		return nil, failure(ctx, err)
	}
	views, err := connectionViews(connections)
	if err != nil {
		return nil, failure(ctx, err)
	}
	return &pb.ListConnectionsResponse{Connections: views}, nil
}

// answer shows the Connection a call read or changed, or passes on the call's error.
func answer(c connection, err error) (*pb.Connection, error) {
	if err != nil {
		return nil, err
	}
	return connectionView(c)
}

func (s *Service) GetConnection(ctx context.Context, request *pb.GetConnectionRequest) (*pb.GetConnectionResponse, error) {
	id, err := parseConnectionID(request.GetConnectionId())
	if err != nil {
		return nil, err
	}
	view, err := answer(s.store.get(ctx, id))
	if err != nil {
		return nil, failure(ctx, err)
	}
	return &pb.GetConnectionResponse{Connection: view}, nil
}

func (s *Service) UpdateConnection(ctx context.Context, request *pb.UpdateConnectionRequest) (*pb.UpdateConnectionResponse, error) {
	id, err := parseConnectionID(request.GetConnectionId())
	if err != nil {
		return nil, err
	}
	if err := checkVersion("expected_version", request.GetExpectedVersion()); err != nil {
		return nil, err
	}
	name, err := displayName(request.GetDisplayName())
	if err != nil {
		return nil, err
	}
	view, err := answer(s.store.rename(ctx, id, request.GetExpectedVersion(), name))
	if err != nil {
		return nil, failure(ctx, err)
	}
	return &pb.UpdateConnectionResponse{Connection: view}, nil
}

func (s *Service) RebindConnection(ctx context.Context, request *pb.RebindConnectionRequest) (*pb.RebindConnectionResponse, error) {
	id, err := parseConnectionID(request.GetConnectionId())
	if err != nil {
		return nil, err
	}
	if err := checkVersion("expected_version", request.GetExpectedVersion()); err != nil {
		return nil, err
	}
	account, err := parseAccount("service_account", request.GetServiceAccount())
	if err != nil {
		return nil, err
	}
	view, err := answer(s.store.rebind(ctx, id, request.GetExpectedVersion(), account))
	if err != nil {
		return nil, failure(ctx, err)
	}
	return &pb.RebindConnectionResponse{Connection: view}, nil
}

func (s *Service) UnbindConnection(ctx context.Context, request *pb.UnbindConnectionRequest) (*pb.UnbindConnectionResponse, error) {
	id, err := parseConnectionID(request.GetConnectionId())
	if err != nil {
		return nil, err
	}
	if err := checkVersion("expected_version", request.GetExpectedVersion()); err != nil {
		return nil, err
	}
	view, err := answer(s.store.unbind(ctx, id, request.GetExpectedVersion()))
	if err != nil {
		return nil, failure(ctx, err)
	}
	return &pb.UnbindConnectionResponse{Connection: view}, nil
}

func (s *Service) PreviewConnectionEnrollment(ctx context.Context, request *pb.PreviewConnectionEnrollmentRequest) (*pb.PreviewConnectionEnrollmentResponse, error) {
	e, err := s.store.previewEnrollment(ctx, request.GetHandle(), s.operator(ctx))
	if err != nil {
		return nil, failure(ctx, err)
	}
	return &pb.PreviewConnectionEnrollmentResponse{
		ClientId: e.clientID, ClientName: e.clientName, RedirectUri: e.redirectURI,
		ExpiresAt: timestamppb.New(e.expiresAt), Version: e.version,
	}, nil
}

// parseDecision reads the decision the request makes.
func parseDecision(request *pb.DecideConnectionEnrollmentRequest) (decision, error) {
	if err := checkVersion("expected_version", request.GetExpectedVersion()); err != nil {
		return decision{}, err
	}
	d := decision{expectedVersion: request.GetExpectedVersion()}
	switch choice := request.GetDecision().(type) {
	case *pb.DecideConnectionEnrollmentRequest_Deny:
		return d, nil
	case *pb.DecideConnectionEnrollmentRequest_Allow:
		account, err := parseAccount("allow.service_account", choice.Allow.GetServiceAccount())
		if err != nil {
			return decision{}, err
		}
		d.allow = &allowance{account: account}
		switch connection := choice.Allow.GetConnection().(type) {
		case *pb.EnrollmentAllow_NewConnection:
			name, err := displayName(connection.NewConnection.GetDisplayName())
			if err != nil {
				return decision{}, err
			}
			d.allow.connection = newConnection{displayName: name}
		case *pb.EnrollmentAllow_Reconnect:
			reconnect := connection.Reconnect
			id, err := parseConnectionID(reconnect.GetConnectionId())
			if err != nil {
				return decision{}, err
			}
			if err := checkVersion("reconnect.expected_version", reconnect.GetExpectedVersion()); err != nil {
				return decision{}, err
			}
			if !reconnect.GetAuthorityChangeConfirmed() {
				return decision{}, invalid("a reconnect replaces the Connection's authority: confirm the change")
			}
			d.allow.connection = reconnection{id: id, expectedVersion: reconnect.GetExpectedVersion()}
		default:
			return decision{}, invalid("an allow names a new Connection or one to reconnect")
		}
		return d, nil
	default:
		return decision{}, invalid("a decision is an allow or a deny")
	}
}

func (s *Service) DecideConnectionEnrollment(ctx context.Context, request *pb.DecideConnectionEnrollmentRequest) (*pb.DecideConnectionEnrollmentResponse, error) {
	d, err := parseDecision(request)
	if err != nil {
		return nil, err
	}
	result, err := s.store.decideEnrollment(ctx, request.GetHandle(), d, s.operator(ctx))
	if err != nil {
		return nil, failure(ctx, err)
	}
	decided, known := verdicts[result.verdict]
	if !known {
		return nil, failure(ctx, fmt.Errorf("%w: enrollment verdict %q", errStored, result.verdict))
	}
	return &pb.DecideConnectionEnrollmentResponse{Verdict: decided, RedirectUrl: result.redirectURL}, nil
}
