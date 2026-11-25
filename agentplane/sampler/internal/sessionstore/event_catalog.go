package sessionstore

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/binary"
	"encoding/hex"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/eventcatalog"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore/internal/contentdb"
	"google.golang.org/protobuf/proto"
)

// The first byte has invalid protobuf wire type 7. The private envelope survives
// journal compression and packing and names an exact-byte sampler_content row.
const (
	catalogMarker     = "\x0fCAT\x01"
	catalogKeyEnd     = len(catalogMarker) + sha256.Size
	catalogHeaderSize = catalogKeyEnd + 4
)

func detachJournalCatalog(ctx context.Context, tx *sql.Tx, entry *eventlogpb.EventEntry, wire []byte) ([]byte, error) {
	shortened, catalog, offset := eventcatalog.Split(entry, wire)
	if catalog == nil {
		return wire, nil
	}
	store := newContentDB(ctx, tx)
	id, err := store.PutContent(content.ContentJavaScriptToolCatalog, catalog)
	if err != nil {
		return nil, err
	}
	if err := store.Save(); err != nil {
		return nil, err
	}
	stored := make([]byte, 0, catalogHeaderSize+len(shortened))
	stored = append(stored, catalogMarker...)
	stored, err = hex.AppendDecode(stored, []byte(id))
	if err != nil {
		return nil, err
	}
	stored = binary.BigEndian.AppendUint32(stored, uint32(offset))
	return append(stored, shortened...), nil
}

func decodeJournalEntries(ctx context.Context, q contentdb.Queryer, frames []journalFrame) ([]*eventlogpb.EventEntry, error) {
	type pendingCatalog struct {
		index  int
		wire   []byte
		id     content.ContentID
		offset int
	}
	var pending []pendingCatalog
	var ids []string
	var seen map[string]bool
	entries := make([]*eventlogpb.EventEntry, len(frames))
	for index, frame := range frames {
		wire, err := eventcodec.Uncompress(frame.wire)
		if err != nil {
			return nil, fmt.Errorf("decompress event at cursor %d: %w", frame.sequence, err)
		}
		if bytes.HasPrefix(wire, []byte(catalogMarker)) {
			if len(wire) < catalogHeaderSize {
				return nil, fmt.Errorf("truncated tool catalog reference")
			}
			id := hex.EncodeToString(wire[len(catalogMarker):catalogKeyEnd])
			offset := int(binary.BigEndian.Uint32(wire[catalogKeyEnd:catalogHeaderSize]))
			pending = append(pending, pendingCatalog{index, wire[catalogHeaderSize:], content.ContentID(id), offset})
			if seen == nil {
				seen = make(map[string]bool)
			}
			if !seen[id] {
				ids = append(ids, id)
				seen[id] = true
			}
			continue
		}
		entry, err := decodeJournalEntry(wire, frame.sequence)
		if err != nil {
			return nil, err
		}
		entries[index] = entry
	}
	if len(ids) == 0 {
		return entries, nil
	}
	catalogs, err := contentdb.ReadContent(ctx, q, ids)
	if err != nil {
		return nil, err
	}
	for _, ref := range pending {
		catalog, ok := catalogs[ref.id]
		if !ok || catalog.Kind != content.ContentJavaScriptToolCatalog {
			return nil, fmt.Errorf("event has no stored tool catalog for %s", ref.id)
		}
		wire, err := eventcatalog.Restore(ref.wire, catalog.Payload, ref.offset)
		if err != nil {
			return nil, err
		}
		entry, err := decodeJournalEntry(wire, frames[ref.index].sequence)
		if err != nil {
			return nil, err
		}
		entries[ref.index] = entry
	}
	return entries, nil
}

func decodeJournalEntry(wire []byte, sequence uint64) (*eventlogpb.EventEntry, error) {
	entry := &eventlogpb.EventEntry{}
	if err := proto.Unmarshal(wire, entry); err != nil {
		return nil, fmt.Errorf("decode event at cursor %d: %w", sequence, err)
	}
	if entry.GetCursor() != sequence {
		return nil, fmt.Errorf("event at cursor %d contains cursor %d", sequence, entry.GetCursor())
	}
	return entry, nil
}
