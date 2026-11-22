package threads

import (
	"context"
	"encoding/base64"
	"log/slog"
	"strconv"
	"strings"
	"unicode/utf8"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	threadstore "github.com/agentydragon/ducktape/agentplane/app/internal/threads"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type NoticeStore interface {
	AcknowledgeNotice(context.Context, uuid.UUID, threadstore.NoticeRevision) error
	FindNoticeAcknowledgements(context.Context, uuid.UUID, []threadstore.NoticeRevision) ([]threadstore.NoticeAcknowledgement, error)
	ListNoticeAcknowledgements(context.Context, uuid.UUID, *int64, int) ([]threadstore.NoticeAcknowledgement, error)
}

func noticeRevision(n *apppb.CommandNoticeRevision) (threadstore.NoticeRevision, error) {
	id := n.GetCommandId()
	if id == "" || len(id) > 1024 || !utf8.ValidString(id) || strings.ContainsRune(id, 0) || n.GetReceiptVersion() <= 0 {
		return threadstore.NoticeRevision{}, status.Error(codes.InvalidArgument, "A notice requires a command ID of 1 to 1024 bytes and a positive Dispatcher receipt version")
	}
	return threadstore.NoticeRevision{CommandID: id, Version: n.GetReceiptVersion()}, nil
}

func (h *Handler) noticeSession(ctx context.Context, threadID string) (uuid.UUID, error) {
	id, err := threadTargetID(threadID)
	if err != nil {
		return uuid.Nil, err
	}
	if h.dispatcher == nil {
		return uuid.Nil, status.Error(codes.Unavailable, "Session lookup is unavailable")
	}
	response, err := h.dispatcher.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: threadID})
	if err != nil {
		return uuid.Nil, err
	}
	if response.GetSession().GetSessionId() != threadID {
		return uuid.Nil, status.Error(codes.Unavailable, "Session lookup did not return the requested Session")
	}
	return id, nil
}

func (h *Handler) AcknowledgeCommandNotice(ctx context.Context, request *apppb.AcknowledgeCommandNoticeRequest) (*emptypb.Empty, error) {
	n, err := noticeRevision(request.GetNotice())
	if err != nil {
		return nil, err
	}
	id, err := h.noticeSession(ctx, request.GetThreadId())
	if err != nil {
		return nil, err
	}
	if err := h.store.AcknowledgeNotice(ctx, id, n); err != nil {
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
	id, err := h.noticeSession(ctx, request.GetThreadId())
	if err != nil {
		return nil, err
	}
	rows, err := h.store.FindNoticeAcknowledgements(ctx, id, notices)
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
	var before *int64
	if token := request.GetPageToken(); token != "" {
		raw, err := base64.RawURLEncoding.DecodeString(token)
		scope, key, ok := strings.Cut(string(raw), "/")
		n, parseErr := strconv.ParseInt(key, 10, 64)
		if err != nil || !ok || scope != request.GetThreadId() || parseErr != nil || n <= 0 {
			return nil, status.Error(codes.InvalidArgument, "Invalid notice history page token for this Session")
		}
		before = &n
	}
	id, err := h.noticeSession(ctx, request.GetThreadId())
	if err != nil {
		return nil, err
	}
	rows, err := h.store.ListNoticeAcknowledgements(ctx, id, before, int(size))
	if err != nil {
		return nil, noticeStorageError(err)
	}
	var next *string
	if len(rows) > int(size) {
		rows = rows[:size]
		token := base64.RawURLEncoding.EncodeToString([]byte(id.String() + "/" + strconv.FormatInt(rows[len(rows)-1].ID, 10)))
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
