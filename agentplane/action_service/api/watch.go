package api

import (
	"bytes"
	"cmp"
	"context"
	"errors"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

const (
	// snapshotChunk bounds the requests in one snapshot frame.
	snapshotChunk = 64
	// recheckInterval is how often a watch with nothing to send checks its bearer again.
	recheckInterval = 5 * time.Second
	// watchWriteTimeout bounds waiting for a watch Send to clear transport backpressure.
	watchWriteTimeout = 5 * time.Second
)

// inFlight are the states a request leaves to satisfy WATCH_UNTIL_TERMINAL.
var inFlight = []action_servicepb.ActionState{action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, action_servicepb.ActionState_ACTION_STATE_ALLOWED, action_servicepb.ActionState_ACTION_STATE_DISPATCHING, action_servicepb.ActionState_ACTION_STATE_RUNNING}

// satisfies reads a watch's `until`: whether a receipt in a state ends the watch.
func satisfies(until action_servicepb.WatchUntil) (func(action_servicepb.ActionState) bool, error) {
	switch until {
	case action_servicepb.WatchUntil_WATCH_UNTIL_DECISION:
		return func(state action_servicepb.ActionState) bool {
			return state != action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING
		}, nil
	case action_servicepb.WatchUntil_WATCH_UNTIL_UNSPECIFIED, action_servicepb.WatchUntil_WATCH_UNTIL_TERMINAL:
		return func(state action_servicepb.ActionState) bool { return !slices.Contains(inFlight, state) }, nil
	default:
		return nil, invalidRequest("until %v is not a watch condition", until)
	}
}

// follower is a watch's subscription to the changes of what it shows: the commits of requests or linkages, or the
// catalog's next change.
type follower struct {
	gate    *Gate
	updates <-chan struct{}
	// due fires when what the watch shows changes with the clock alone; nil when nothing does.
	due <-chan time.Time
}

// follow subscribes to the commits of requests. The watch reads after it, so a commit in between is not missed.
func (s *Service) follow() (follower, func(), error) {
	updates, unsubscribe, err := s.updates.Subscribe(store.UpdatesChannel)
	if err != nil {
		return follower{}, nil, err
	}
	return follower{gate: s.gate, updates: updates}, unsubscribe, nil
}

// changed blocks until a change may have changed what the watch shows: a wakeup, or due. The wakeup that follows a
// reconnection of the LISTEN connection is one, so a commit made while it was down is read late but not missed.
// Between changes it checks the bearer again every recheckInterval. It fails once the bearer is no longer accepted,
// the service drains (errDraining), or the call ends.
func (f follower) changed(ctx context.Context) error {
	recheck := time.NewTicker(recheckInterval)
	defer recheck.Stop()
	for {
		select {
		case <-f.updates:
			return nil
		case <-f.due:
			return nil
		case <-f.gate.DrainingContext().Done():
			return errDraining
		case <-ctx.Done():
			return ctx.Err()
		case <-recheck.C:
			if err := f.gate.Recheck(ctx); err != nil {
				return err
			}
		}
	}
}

// send checks the bearer again, then sends the frame.
func send[Frame any](ctx, writeCtx context.Context, gate *Gate, stream grpc.ServerStreamingServer[Frame], frame *Frame) error {
	if err := gate.Recheck(ctx); err != nil {
		return err
	}
	err := streaming.SendWithin(writeCtx, watchWriteTimeout, stream.Send, frame)
	switch {
	case errors.Is(err, streaming.ErrSendTimeout):
		return status.Error(codes.DeadlineExceeded, "action watch write deadline expired")
	case errors.Is(err, context.Canceled) && ctx.Err() != nil:
		return status.FromContextError(ctx.Err()).Err()
	case errors.Is(err, context.Canceled) && gate.DrainingContext().Err() != nil:
		return errDraining
	case errors.Is(err, context.Canceled):
		return status.FromContextError(context.Canceled).Err()
	case errors.Is(err, context.DeadlineExceeded):
		return status.FromContextError(context.DeadlineExceeded).Err()
	default:
		return err
	}
}

func watchWriteContext(ctx context.Context, gate *Gate) (context.Context, func()) {
	writeCtx, cancel := context.WithCancel(ctx)
	stop := context.AfterFunc(gate.DrainingContext(), cancel)
	return writeCtx, func() {
		stop()
		cancel()
	}
}

func (s *Service) WatchActionRequest(request *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
	ctx := stream.Context()
	writeCtx, stopWrites := watchWriteContext(ctx, s.gate)
	defer stopWrites()
	id, err := parseRequestID(request.GetRequestId())
	if err != nil {
		return err
	}
	satisfied, err := satisfies(request.GetUntil())
	if err != nil {
		return err
	}
	f, unsubscribe, err := s.follow()
	if err != nil {
		return err
	}
	defer unsubscribe()
	// Versions start at 1, so the first read is always sent.
	var sent int64
	for {
		found, err := s.store.Get(ctx, id, admitted(ctx).principal)
		if err != nil {
			return err
		}
		if found.Version != sent {
			view, err := requestView(found)
			if err != nil {
				return err
			}
			if err := send(ctx, writeCtx, s.gate, stream, &action_servicepb.WatchActionRequestResponse{ActionRequest: view}); err != nil {
				return err
			}
			sent = found.Version
		}
		if satisfied(found.State) {
			return nil
		}
		if err := f.changed(ctx); err != nil {
			return err
		}
	}
}

func (s *Service) WatchActionRequests(request *action_servicepb.WatchActionRequestsRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestsResponse]) error {
	ctx := stream.Context()
	states, err := requestedStates(request.GetState())
	if err != nil {
		return err
	}
	f, unsubscribe, err := s.follow()
	if err != nil {
		return err
	}
	defer unsubscribe()
	writeCtx, stopWrites := watchWriteContext(ctx, s.gate)
	defer stopWrites()
	w := setWatch{service: s, ctx: ctx, writeCtx: writeCtx, stream: stream, principal: admitted(ctx).principal, states: states}
	if err := w.snapshot(); err != nil {
		return err
	}
	for {
		if err := f.changed(ctx); err != nil {
			if errors.Is(err, errDraining) {
				// A planned renewal: the client connects again, to a replica that is not going away.
				return w.sendReconnect(&action_servicepb.WatchActionRequestsResponse{
					Frame: &action_servicepb.WatchActionRequestsResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}},
				})
			}
			return err
		}
		if err := w.upsertChanges(); err != nil {
			return err
		}
	}
}

// setWatch is one WatchActionRequests stream.
type setWatch struct {
	service   *Service
	ctx       context.Context
	writeCtx  context.Context
	stream    grpc.ServerStreamingServer[action_servicepb.WatchActionRequestsResponse]
	principal store.ReadPrincipal
	// states is the filter; empty selects every state.
	states []action_servicepb.ActionState
	// shown is the version the client holds of each request in the filtered set.
	shown map[uuid.UUID]int64
}

func (w *setWatch) send(frame *action_servicepb.WatchActionRequestsResponse) error {
	return send(w.ctx, w.writeCtx, w.service.gate, w.stream, frame)
}

func (w *setWatch) sendReconnect(frame *action_servicepb.WatchActionRequestsResponse) error {
	return send(w.ctx, w.ctx, w.service.gate, w.stream, frame)
}

func (w *setWatch) selects(state action_servicepb.ActionState) bool {
	return len(w.states) == 0 || slices.Contains(w.states, state)
}

// snapshot sends the filtered set as snapshot frames of at most snapshotChunk requests, then snapshot_complete.
func (w *setWatch) snapshot() error {
	w.shown = map[uuid.UUID]int64{}
	chunk := snapshotChunk
	page := store.ListFilter{States: w.states, Limit: &chunk}
	for {
		requests, err := w.service.store.List(w.ctx, w.principal, page)
		if err != nil {
			return err
		}
		if len(requests) > 0 {
			views, err := requestViews(requests)
			if err != nil {
				return err
			}
			if err := w.send(&action_servicepb.WatchActionRequestsResponse{Frame: &action_servicepb.WatchActionRequestsResponse_Snapshot{
				Snapshot: &action_servicepb.ActionRequestChunk{ActionRequests: views},
			}}); err != nil {
				return err
			}
			for _, request := range requests {
				w.shown[request.ID] = request.Version
			}
		}
		if len(requests) < chunk {
			break
		}
		page.AfterID = &requests[len(requests)-1].ID
	}
	return w.send(&action_servicepb.WatchActionRequestsResponse{
		Frame: &action_servicepb.WatchActionRequestsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}},
	})
}

// upsertChanges sends an upsert of each request whose version is not the one the client holds: a request that
// entered the filtered set or changed in it, and one that left it, which the client drops. Only those are read.
func (w *setWatch) upsertChanges() error {
	versions, err := w.service.store.Versions(w.ctx, w.principal, w.states)
	if err != nil {
		return err
	}
	var changed []uuid.UUID
	for id, version := range versions {
		if w.shown[id] != version {
			changed = append(changed, id)
		}
	}
	for id := range w.shown {
		if _, still := versions[id]; !still {
			changed = append(changed, id)
		}
	}
	if len(changed) == 0 {
		return nil
	}
	requests, err := w.service.store.Find(w.ctx, w.principal, changed)
	if err != nil {
		return err
	}
	slices.SortFunc(requests, func(a, b store.ActionRequest) int {
		return cmp.Or(a.UpdatedAt.Compare(b.UpdatedAt), bytes.Compare(a.ID[:], b.ID[:]))
	})
	for _, request := range requests {
		view, err := requestView(request)
		if err != nil {
			return err
		}
		if err := w.send(&action_servicepb.WatchActionRequestsResponse{
			Frame: &action_servicepb.WatchActionRequestsResponse_Upsert{Upsert: view},
		}); err != nil {
			return err
		}
		// The request is read after its version, so it may have left the set since.
		if w.selects(request.State) {
			w.shown[request.ID] = request.Version
		} else {
			delete(w.shown, request.ID)
		}
	}
	return nil
}
