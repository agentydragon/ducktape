package threads

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"strconv"
	"strings"
	"unicode/utf8"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/agentydragon/ducktape/agentplane/app/internal/auth"
	threadstore "github.com/agentydragon/ducktape/agentplane/app/internal/threads"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type NoticeStore interface {
	AcknowledgeNotice(context.Context, threadstore.NoticeScope, threadstore.NoticeRevision) error
	FindNoticeAcknowledgements(context.Context, threadstore.NoticeScope, []threadstore.NoticeRevision) ([]threadstore.NoticeAcknowledgement, error)
	ListNoticeAcknowledgements(context.Context, threadstore.NoticeScope, *int64, int) ([]threadstore.NoticeAcknowledgement, error)
}

func noticeRevision(n *apppb.CommandNoticeRevision) (threadstore.NoticeRevision, error) {
	id := n.GetCommandId()
	if id == "" || len(id) > 1024 || !utf8.ValidString(id) || strings.ContainsRune(id, 0) || n.GetReceiptVersion() <= 0 {
		return threadstore.NoticeRevision{}, status.Error(codes.InvalidArgument, "A notice requires a command ID of 1 to 1024 bytes and a positive Dispatcher receipt version")
	}
	return threadstore.NoticeRevision{CommandID: id, Version: n.GetReceiptVersion()}, nil
}

func (h *Handler) noticeScope(ctx context.Context, threadID string) (threadstore.NoticeScope, error) {
	id, err := threadTargetID(threadID)
	if err != nil {
		return threadstore.NoticeScope{}, err
	}
	if h.dispatcher == nil {
		return threadstore.NoticeScope{}, status.Error(codes.Unavailable, "Session lookup is unavailable")
	}
	response, err := h.dispatcher.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: threadID})
	if err != nil {
		return threadstore.NoticeScope{}, err
	}
	if response.GetSession().GetSessionId() != threadID {
		return threadstore.NoticeScope{}, status.Error(codes.Unavailable, "Session lookup did not return the requested Session")
	}
	caller, ok := auth.CallerFromContext(ctx)
	if !ok || caller.Kind != auth.CallerOperator || caller.Issuer == "" || caller.Subject == "" {
		return threadstore.NoticeScope{}, status.Error(codes.Unauthenticated, "Verified operator identity is required")
	}
	// JSON encodes the two strings without delimiter collisions. Usernames and session cookies may change.
	identity, _ := json.Marshal([2]string{caller.Issuer, caller.Subject})
	return threadstore.NoticeScope{ThreadID: id, OperatorKey: sha256.Sum256(identity)}, nil
}

func (h *Handler) AcknowledgeCommandNotice(ctx context.Context, request *apppb.AcknowledgeCommandNoticeRequest) (*emptypb.Empty, error) {
	n, err := noticeRevision(request.GetNotice())
	if err != nil {
		return nil, err
	}
	scope, err := h.noticeScope(ctx, request.GetThreadId())
	if err != nil {
		return nil, err
	}
	if err := h.store.AcknowledgeNotice(ctx, scope, n); err != nil {
		return nil, noticeStorageError(err)
	}
	return &emptypb.Empty{}, nil
}

func (h *Handler) BatchGetCommandNoticeAcknowledgements(ctx context.Context, request *apppb.BatchGetCommandNoticeAcknowledgementsRequest) (*apppb.CommandNoticeAcknowledgements, error) {
	if len(request.GetNotices()) == 0 || len(request.GetNotices()) > threadstore.MaxNoticePage {
		return nil, status.Errorf(codes.InvalidArgument, "Expected 1 to %d notices", threadstore.MaxNoticePage)
	}
	notices := make([]threadstore.NoticeRevision, 0, len(request.GetNotices()))
	for _, n := range request.GetNotices() {
		notice, err := noticeRevision(n)
		if err != nil {
			return nil, err
		}
		notices = append(notices, notice)
	}
	scope, err := h.noticeScope(ctx, request.GetThreadId())
	if err != nil {
		return nil, err
	}
	rows, err := h.store.FindNoticeAcknowledgements(ctx, scope, notices)
	if err != nil {
		return nil, noticeStorageError(err)
	}
	return noticeAcknowledgements(rows), nil
}

func (h *Handler) ListCommandNoticeAcknowledgements(ctx context.Context, request *apppb.ListCommandNoticeAcknowledgementsRequest) (*apppb.CommandNoticeAcknowledgements, error) {
	size := request.GetPageSize()
	if size > threadstore.MaxNoticePage {
		return nil, status.Errorf(codes.InvalidArgument, "Page size exceeds %d", threadstore.MaxNoticePage)
	}
	if size == 0 {
		size = 25
	}
	scope, err := h.noticeScope(ctx, request.GetThreadId())
	if err != nil {
		return nil, err
	}
	scopeDigest := sha256.Sum256(append(scope.OperatorKey[:], scope.ThreadID[:]...))
	pageScope := hex.EncodeToString(scopeDigest[:]) + "/"
	var before *int64
	if token := request.GetPageToken(); token != "" {
		if len(token) > base64.RawURLEncoding.EncodedLen(len(pageScope)+19) {
			return nil, status.Error(codes.InvalidArgument, "Invalid notice history page token")
		}
		raw, err := base64.RawURLEncoding.DecodeString(token)
		key, ok := strings.CutPrefix(string(raw), pageScope)
		n, parseErr := strconv.ParseInt(key, 10, 64)
		if err != nil || !ok || parseErr != nil || n <= 0 {
			return nil, status.Error(codes.InvalidArgument, "Invalid notice history page token for this operator and Session")
		}
		before = &n
	}
	rows, err := h.store.ListNoticeAcknowledgements(ctx, scope, before, int(size))
	if err != nil {
		return nil, noticeStorageError(err)
	}
	var next *string
	if len(rows) > int(size) {
		rows = rows[:size]
		token := base64.RawURLEncoding.EncodeToString([]byte(pageScope + strconv.FormatInt(rows[len(rows)-1].ID, 10)))
		next = &token
	}
	result := noticeAcknowledgements(rows)
	result.NextPageToken = next
	return result, nil
}

func noticeAcknowledgements(rows []threadstore.NoticeAcknowledgement) *apppb.CommandNoticeAcknowledgements {
	result := &apppb.CommandNoticeAcknowledgements{}
	for _, r := range rows {
		result.Acknowledgements = append(result.Acknowledgements, &apppb.CommandNoticeAcknowledgement{
			Notice:         &apppb.CommandNoticeRevision{CommandId: r.CommandID, ReceiptVersion: r.Version},
			AcknowledgedAt: timestamppb.New(r.AcknowledgedAt),
		})
	}
	return result
}

func noticeStorageError(err error) error {
	slog.Error("Command notice acknowledgement storage failed", "error", err)
	return status.Error(codes.Internal, "Command notice acknowledgement storage unavailable")
}
