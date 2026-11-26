package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"fmt"
	"sort"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
)

type JournalOwnerInventory struct {
	Owner             string
	Sessions          int64
	OrdinaryRows      int64
	LegacyGzipRows    int64
	CurrentBlocks     int64
	LegacyGzipBlocks  int64
	UnsupportedRows   int64
	InvalidRows       int64
	UnsupportedBlocks int64
	InvalidBlocks     int64
	CoverageGaps      int64
	MissingSequences  int64
	InvalidRanges     int64
}

type JournalClassification string

const (
	journalOrdinary    JournalClassification = "ordinary"
	journalCurrent     JournalClassification = "current"
	journalLegacyGzip  JournalClassification = "legacy_gzip"
	journalUnsupported JournalClassification = "unsupported"
	journalInvalid     JournalClassification = "invalid"
	journalGap         JournalClassification = "gap"
)

type JournalInventoryIssue struct {
	Owner, SessionID, Storage string
	Classification            JournalClassification
	First, Last               uint64
	Reason                    string
}

type JournalInventoryResult struct {
	Owners []JournalOwnerInventory
	Issues []JournalInventoryIssue
}

type journalCoverage struct {
	owner, sessionID string
	cursor, next     uint64
}

// VerifyJournalInventory scans one repeatable-read snapshot of every retained
// Session and journal row. It does not skip based on repack state or skip busy Sessions.
func (s *Store) VerifyJournalInventory(ctx context.Context) (JournalInventoryResult, error) {
	var result JournalInventoryResult
	err := postgres.Transaction(ctx, s.db, sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead}, func(tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, `
			SELECT COALESCE(s.service_spiffe_id,s.account_uid),s.id,'session',0,s.cursor,NULL::BYTEA
			FROM sampler_session s
			UNION ALL
			SELECT COALESCE(s.service_spiffe_id,s.account_uid),s.id,'row',e.sequence,e.sequence,e.entry
			FROM sampler_session s JOIN sampler_event e ON e.session_id=s.id
			UNION ALL
			SELECT COALESCE(s.service_spiffe_id,s.account_uid),s.id,'block',b.first_sequence,b.last_sequence,b.payload
			FROM sampler_session s JOIN sampler_event_block b ON b.session_id=s.id
			ORDER BY 1,2,4`)
		if err != nil {
			return err
		}
		defer rows.Close()
		owners := make(map[string]*JournalOwnerInventory)
		var coverage *journalCoverage
		recordGap := func(owner *JournalOwnerInventory, sessionID string, first, last uint64) {
			if last < first {
				return
			}
			owner.CoverageGaps++
			owner.MissingSequences += int64(last - first + 1)
			result.Issues = append(result.Issues, JournalInventoryIssue{
				Owner: owner.Owner, SessionID: sessionID,
				Storage: "coverage", Classification: journalGap, First: first, Last: last, Reason: "journal cursor range has no stored event",
			})
		}
		recordInvalidRange := func(owner *JournalOwnerInventory, sessionID, storage, reason string, first, last uint64) {
			owner.InvalidRanges++
			result.Issues = append(result.Issues, JournalInventoryIssue{
				Owner: owner.Owner, SessionID: sessionID,
				Storage: storage, Classification: journalInvalid, First: first, Last: last, Reason: reason,
			})
		}
		finishCoverage := func() {
			if coverage == nil || coverage.next > coverage.cursor {
				return
			}
			recordGap(owners[coverage.owner], coverage.sessionID, coverage.next, coverage.cursor)
		}
		for rows.Next() {
			var owner, sessionID, kind string
			var first, last uint64
			var payload []byte
			if err := rows.Scan(&owner, &sessionID, &kind, &first, &last, &payload); err != nil {
				return err
			}
			stat := owners[owner]
			if stat == nil {
				stat = &JournalOwnerInventory{Owner: owner}
				owners[owner] = stat
			}
			switch kind {
			case "session":
				finishCoverage()
				stat.Sessions++
				coverage = &journalCoverage{owner: owner, sessionID: sessionID, cursor: last, next: 1}
			case "row":
				if err := inspectJournalCoverage(stat, coverage, owner, sessionID, first, last, recordGap, recordInvalidRange); err != nil {
					return err
				}
				classification, reason := classifyJournalRow(payload, first)
				switch classification {
				case journalOrdinary:
					stat.OrdinaryRows++
				case journalLegacyGzip:
					stat.LegacyGzipRows++
				case journalUnsupported:
					stat.UnsupportedRows++
				case journalInvalid:
					stat.InvalidRows++
				}
				if classification == journalUnsupported || classification == journalInvalid {
					result.Issues = append(result.Issues, JournalInventoryIssue{
						Owner: owner, SessionID: sessionID,
						Storage: "sampler_event", Classification: classification, First: first, Last: last, Reason: reason,
					})
				}
			case "block":
				if err := inspectJournalCoverage(stat, coverage, owner, sessionID, first, last, recordGap, recordInvalidRange); err != nil {
					return err
				}
				classification, reason := classifyJournalBlock(payload, first, last)
				switch classification {
				case journalCurrent:
					stat.CurrentBlocks++
				case journalLegacyGzip:
					stat.LegacyGzipBlocks++
				case journalUnsupported:
					stat.UnsupportedBlocks++
				case journalInvalid:
					stat.InvalidBlocks++
				}
				if classification == journalUnsupported || classification == journalInvalid {
					result.Issues = append(result.Issues, JournalInventoryIssue{
						Owner: owner, SessionID: sessionID,
						Storage: "sampler_event_block", Classification: classification, First: first, Last: last, Reason: reason,
					})
				} else if classification == journalLegacyGzip {
					result.Issues = append(result.Issues, JournalInventoryIssue{
						Owner: owner, SessionID: sessionID,
						Storage: "sampler_event_block", Classification: classification, First: first, Last: last,
						Reason: "legacy gzip block requires repacking",
					})
				}
			default:
				return fmt.Errorf("unknown journal inventory record kind %q", kind)
			}
		}
		if err := rows.Err(); err != nil {
			return err
		}
		finishCoverage()
		for _, owner := range owners {
			result.Owners = append(result.Owners, *owner)
		}
		sort.Slice(result.Owners, func(i, j int) bool { return result.Owners[i].Owner < result.Owners[j].Owner })
		return nil
	})
	if err != nil {
		return JournalInventoryResult{}, err
	}
	return result, nil
}

func inspectJournalCoverage(
	owner *JournalOwnerInventory,
	coverage *journalCoverage,
	ownerName, sessionID string,
	first, last uint64,
	recordGap func(*JournalOwnerInventory, string, uint64, uint64),
	recordInvalidRange func(*JournalOwnerInventory, string, string, string, uint64, uint64),
) error {
	if coverage == nil || coverage.owner != ownerName || coverage.sessionID != sessionID {
		return fmt.Errorf("journal inventory lost Session ordering at %q", sessionID)
	}
	if first > coverage.next && first <= coverage.cursor {
		recordGap(owner, sessionID, coverage.next, first-1)
	}
	if first < coverage.next {
		recordInvalidRange(owner, sessionID, "journal coverage", "stored cursor range overlaps prior storage", first, last)
	}
	if last > coverage.cursor {
		recordInvalidRange(owner, sessionID, "journal coverage", "stored cursor range extends past the Session cursor", first, last)
	}
	if last >= coverage.next {
		coverage.next = last + 1
	}
	return nil
}

func classifyJournalRow(payload []byte, sequence uint64) (JournalClassification, string) {
	isLegacy := isGzipJournalPayload(payload)
	if eventblock.IsBlock(payload) || bytes.HasPrefix(payload, []byte{0x28, 0xb5, 0x2f, 0xfd}) {
		return journalUnsupported, "block-compressed payload stored in a standalone event row"
	}
	frame, err := eventcodec.UnpackPackableEntry(payload)
	if err != nil {
		return journalInvalid, "standalone event decompression failed: " + err.Error()
	}
	if len(frame) == 0 {
		return journalUnsupported, "standalone event exceeds the bounded inventory decode limit"
	}
	var entry eventlogpb.EventEntry
	if err := eventcodec.Decode(frame, &entry); err != nil {
		return journalInvalid, "standalone event protobuf is invalid"
	}
	if entry.GetCursor() != sequence {
		return journalInvalid, fmt.Sprintf("stored cursor %d differs from row sequence %d", entry.GetCursor(), sequence)
	}
	if isLegacy {
		return journalLegacyGzip, ""
	}
	return journalOrdinary, ""
}

func classifyJournalBlock(payload []byte, first, last uint64) (JournalClassification, string) {
	if last < first {
		return journalInvalid, "block interval is reversed"
	}
	count := last - first + 1
	var frames [][]byte
	switch {
	case eventblock.IsBlock(payload):
		if !bytes.HasPrefix(payload, []byte("APB\x01")) {
			return journalUnsupported, "unknown APB block version"
		}
		decoded, err := eventblock.Decode(payload)
		if err != nil {
			return journalInvalid, "APB v1 block is malformed: " + err.Error()
		}
		frames = decoded
	case isGzipJournalPayload(payload):
		decoded, err := eventcodec.UnpackBlock(payload, int(count))
		if err != nil {
			return journalInvalid, "legacy gzip block is malformed: " + err.Error()
		}
		frames = decoded
	default:
		return journalUnsupported, "unknown event block encoding"
	}
	if uint64(len(frames)) != count {
		return journalInvalid, fmt.Sprintf("block interval contains %d entries but payload contains %d frames", count, len(frames))
	}
	for i, frame := range frames {
		var entry eventlogpb.EventEntry
		if err := eventcodec.Decode(frame, &entry); err != nil {
			return journalInvalid, fmt.Sprintf("event frame %d in block is invalid", i)
		}
		sequence := first + uint64(i)
		if entry.GetCursor() != sequence {
			return journalInvalid, fmt.Sprintf("event frame %d cursor %d differs from sequence %d", i, entry.GetCursor(), sequence)
		}
	}
	if eventblock.IsBlock(payload) {
		return journalCurrent, ""
	}
	return journalLegacyGzip, ""
}

func isGzipJournalPayload(payload []byte) bool {
	return len(payload) >= 2 && payload[0] == 0x1f && payload[1] == 0x8b
}

// JournalInventoryError rejects proof when any legacy block or unclassified
// storage remains. Valid standalone gzip rows are reported separately.
func JournalInventoryError(result JournalInventoryResult) error {
	var legacy, unsupported, invalid, invalidRanges int64
	for _, owner := range result.Owners {
		legacy += owner.LegacyGzipBlocks
		unsupported += owner.UnsupportedBlocks + owner.UnsupportedRows
		invalid += owner.InvalidBlocks + owner.InvalidRows
		invalidRanges += owner.InvalidRanges
	}
	if legacy == 0 && unsupported == 0 && invalid == 0 && invalidRanges == 0 {
		return nil
	}
	return fmt.Errorf("journal verification failed: legacy_gzip_blocks=%d unsupported_records=%d invalid_records=%d invalid_ranges=%d",
		legacy, unsupported, invalid, invalidRanges)
}
