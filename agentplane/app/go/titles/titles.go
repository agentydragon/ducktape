package titles

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"sort"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/app/go/historywatch"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const historyPageSize uint32 = 1000

// History is the read and watch surface needed from one dedicated History Service client. The
// methods exchange generated messages; an adapter can wrap HistoryServiceClient while owning a
// private channel for Redial.
type History interface {
	historywatch.Client
	ReadSessionEvents(context.Context, *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error)
	ReadSessionObservations(context.Context, *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error)
}

// Store is the title-related subset of threads.Store.
type Store interface {
	GetThread(context.Context, uuid.UUID) (*threads.Thread, error)
	TitleContext(context.Context, uuid.UUID) (int, *string, error)
	PendingAutoTitles(context.Context) ([]threads.PendingTitle, error)
	AdvanceTitleScanCursor(context.Context, uuid.UUID, int64) error
	ClaimAutoTitle(context.Context, uuid.UUID, int64) (*int, error)
	GeneratedTitle(context.Context, uuid.UUID, string, int) (threads.Thread, error)
	ApplyFirstMessageTitle(context.Context, uuid.UUID, string, int) (bool, error)
	FailAutoTitle(context.Context, uuid.UUID, int) error
}

// Backend generates a title and reports whether the backend is configured.
type Backend interface {
	Enabled() bool
	Generate(context.Context, []string) (string, error)
}

// ThreadTitles implements on-demand and first-confirmed-message title generation.
type ThreadTitles struct {
	generator Backend
	store     Store
	history   History
	logger    *slog.Logger
}

// NewThreadTitles creates the title service over the existing thread metadata store and History APIs.
func NewThreadTitles(generator Backend, store Store, history History, logger *slog.Logger) (*ThreadTitles, error) {
	if generator == nil || store == nil || history == nil {
		return nil, errors.New("title generator, thread store, and History client are required")
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &ThreadTitles{generator: generator, store: store, history: history, logger: logger}, nil
}

// Enabled reports whether automatic generation is configured.
func (t *ThreadTitles) Enabled() bool { return t != nil && t.generator.Enabled() }

// AutoTitle generates a title from the last twelve confirmed user messages in the bounded History
// window and stores it only if the title revision still matches the one read before generation.
func (t *ThreadTitles) AutoTitle(ctx context.Context, id uuid.UUID) (threads.Thread, error) {
	thread, err := t.store.GetThread(ctx, id)
	if err != nil {
		return threads.Thread{}, err
	}
	if thread == nil {
		return threads.Thread{}, fmt.Errorf("%w: %s", threads.ErrNotFound, id)
	}
	revision, _, err := t.store.TitleContext(ctx, id)
	if err != nil {
		return threads.Thread{}, err
	}
	messages, err := t.confirmedMessages(ctx, id)
	if err != nil {
		return threads.Thread{}, err
	}
	title, err := t.generator.Generate(ctx, messages)
	if err != nil {
		return threads.Thread{}, err
	}
	return t.store.GeneratedTitle(ctx, id, title, revision)
}

// ProcessPending scans enabled first-message title requests and safely claims the first confirmed
// user message before calling the title backend.
func (t *ThreadTitles) ProcessPending(ctx context.Context) error {
	if !t.generator.Enabled() {
		return nil
	}
	pending, err := t.store.PendingAutoTitles(ctx)
	if err != nil {
		return err
	}
	for _, item := range pending {
		confirmed, err := t.firstConfirmed(ctx, item)
		if err != nil {
			return err
		}
		if confirmed == nil {
			continue
		}
		revision, err := t.store.ClaimAutoTitle(ctx, item.ID, confirmed.cursor)
		if err != nil {
			return err
		}
		if revision == nil {
			continue
		}
		title, err := t.generator.Generate(ctx, []string{confirmed.text})
		if err == nil {
			_, err = t.store.ApplyFirstMessageTitle(ctx, item.ID, title, *revision)
		}
		if err != nil {
			if failErr := t.store.FailAutoTitle(ctx, item.ID, *revision); failErr != nil {
				return errors.Join(fmt.Errorf("generate or apply first-message title: %w", err),
					fmt.Errorf("reset first-message title claim: %w", failErr))
			}
			t.logger.WarnContext(ctx, "could not generate a first-message Thread title", "thread_id", item.ID, "error", err)
		}
	}
	return nil
}

type confirmedMessage struct {
	cursor int64
	text   string
}

func (t *ThreadTitles) firstConfirmed(ctx context.Context, pending threads.PendingTitle) (*confirmedMessage, error) {
	if pending.Cursor < 0 {
		return nil, fmt.Errorf("thread %s has a negative title scan cursor", pending.ID)
	}
	cursor := uint64(pending.Cursor)
	for {
		entries, err := t.readEvents(ctx, pending.ID, cursor, historyPageSize)
		if err != nil {
			return nil, err
		}
		for _, entry := range entries {
			confirmed := entry.GetEvent().GetHarnessUserMessageConfirmed()
			if confirmed == nil || strings.TrimSpace(confirmed.GetText()) == "" {
				continue
			}
			if entry.GetCursor() > math.MaxInt64 {
				return nil, fmt.Errorf("History cursor %d exceeds the app's signed cursor range", entry.GetCursor())
			}
			return &confirmedMessage{cursor: int64(entry.GetCursor()), text: confirmed.GetText()}, nil
		}
		if len(entries) < int(historyPageSize) {
			if len(entries) != 0 {
				last := entries[len(entries)-1].GetCursor()
				if last > math.MaxInt64 {
					return nil, fmt.Errorf("History cursor %d exceeds the app's signed cursor range", last)
				}
				if last > cursor {
					if err := t.store.AdvanceTitleScanCursor(ctx, pending.ID, int64(last)); err != nil {
						return nil, err
					}
				}
			}
			return nil, nil
		}
		last := entries[len(entries)-1].GetCursor()
		if last > math.MaxInt64 {
			return nil, fmt.Errorf("History cursor %d exceeds the app's signed cursor range", last)
		}
		if int64(last) > pending.Cursor {
			if err := t.store.AdvanceTitleScanCursor(ctx, pending.ID, int64(last)); err != nil {
				return nil, err
			}
		}
		cursor = last
	}
}

func (t *ThreadTitles) confirmedMessages(ctx context.Context, id uuid.UUID) ([]string, error) {
	watermark, err := t.history.ReadSessionObservations(ctx, &historypb.ReadSessionObservationsRequest{
		SessionId: id.String(), Limit: 1,
	})
	if status.Code(err) == codes.NotFound {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	through := watermark.GetLastCursor()
	after := uint64(0)
	if through > uint64(historyPageSize) {
		after = through - uint64(historyPageSize)
	}
	entries, err := t.readEvents(ctx, id, after, historyPageSize)
	if err != nil {
		return nil, err
	}
	messages := make([]string, 0, maxTitleMessages)
	for _, entry := range entries {
		confirmed := entry.GetEvent().GetHarnessUserMessageConfirmed()
		if confirmed != nil && strings.TrimSpace(confirmed.GetText()) != "" {
			messages = append(messages, confirmed.GetText())
		}
	}
	if len(messages) > maxTitleMessages {
		messages = messages[len(messages)-maxTitleMessages:]
	}
	return messages, nil
}

func (t *ThreadTitles) readEvents(ctx context.Context, id uuid.UUID, after uint64, limit uint32) ([]*eventlogpb.EventEntry, error) {
	entries := make([]*eventlogpb.EventEntry, 0, limit)
	cursor := after
	var through *uint64
	for uint32(len(entries)) < limit {
		requested := limit - uint32(len(entries))
		if through != nil {
			if cursor > *through {
				return nil, errors.New("History Service regressed behind the reader's cursor")
			}
			remaining := *through - cursor
			if remaining < uint64(requested) {
				requested = uint32(remaining)
			}
			if requested == 0 {
				break
			}
		}
		page, err := t.history.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{
			SessionId: id.String(), AfterCursor: cursor, Limit: requested,
		})
		if status.Code(err) == codes.NotFound {
			if through != nil {
				return nil, err
			}
			return entries, nil
		}
		if err != nil {
			return nil, err
		}
		if through == nil {
			watermark := page.GetLastCursor()
			through = &watermark
		}
		if page.GetLastCursor() < *through || cursor > page.GetLastCursor() {
			return nil, errors.New("History Service regressed behind the reader's cursor")
		}
		pageEntries := page.GetEntries()
		if uint32(len(pageEntries)) > requested {
			return nil, errors.New("History Service page exceeds the requested limit")
		}
		unstored := make([]*historypb.CursorRange, 0)
		for _, settlement := range page.GetSettlements() {
			unstored = append(unstored, settlement.GetRanges()...)
		}
		unstored = append(unstored, page.GetRemoved()...)
		sort.Slice(unstored, func(i, j int) bool {
			if unstored[i].GetFirst() == unstored[j].GetFirst() {
				return unstored[i].GetLast() < unstored[j].GetLast()
			}
			return unstored[i].GetFirst() < unstored[j].GetFirst()
		})
		previous := cursor
		for _, entry := range pageEntries {
			if entry.GetCursor() != nextStored(previous, unstored) {
				return nil, errors.New("History Service page has an uncovered cursor gap")
			}
			previous = entry.GetCursor()
		}
		if len(pageEntries) != 0 && pageEntries[len(pageEntries)-1].GetCursor() > page.GetLastCursor() {
			return nil, errors.New("History Service page exceeds its watermark")
		}
		if len(pageEntries) == 0 {
			if nextStored(cursor, unstored) <= *through {
				return nil, errors.New("History Service omitted entries from a published prefix")
			}
			break
		}
		priorCount := len(entries)
		for _, entry := range pageEntries {
			if entry.GetCursor() <= *through {
				entries = append(entries, entry)
			}
		}
		if len(entries) == priorCount {
			break
		}
		cursor = entries[len(entries)-1].GetCursor()
	}
	return entries, nil
}

func nextStored(cursor uint64, ranges []*historypb.CursorRange) uint64 {
	following := cursor + 1
	for _, span := range ranges {
		if span.GetFirst() <= following && following <= span.GetLast() {
			following = span.GetLast() + 1
		}
	}
	return following
}
