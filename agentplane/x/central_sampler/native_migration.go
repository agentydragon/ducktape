package centralsampler

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
)

const nativeMigrationAdvisoryLockID int64 = 7241821

type legacyConversation struct {
	id           string
	api          sql.NullString
	context      []byte
	hasSession   bool
	sessionModel string
	liveLease    bool
}

type convertedLegacyContext struct {
	records       []json.RawMessage
	pendingEffort string
}

// migrateNativeConversations performs all classification, record conversion, API writes, and
// constraint installation in one transaction. If any conversation is ambiguous or malformed,
// PostgreSQL rolls back the whole archive conversion and leaves its records available for retry.
func migrateNativeConversations(ctx context.Context, db *sql.DB, modelAPIs map[string]protocol.APIStyle) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, nativeMigrationAdvisoryLockID); err != nil {
		return fmt.Errorf("lock native conversation migration: %w", err)
	}

	var liveConversation string
	err = tx.QueryRowContext(ctx, `SELECT id::text FROM sampler_conversation
		WHERE api IS NULL AND owner IS NOT NULL AND lease_until > clock_timestamp()
		ORDER BY id LIMIT 1`).Scan(&liveConversation)
	if err == nil {
		return fmt.Errorf("conversation %s has a live lease; stop its session before converting the archive", liveConversation)
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return fmt.Errorf("check for live sampler leases: %w", err)
	}

	conversations, err := loadLegacyConversations(ctx, tx)
	if err != nil {
		return err
	}
	for _, conversation := range conversations {
		if !conversation.api.Valid && conversation.liveLease {
			return fmt.Errorf("conversation %s has a live lease; stop its session before converting the archive", conversation.id)
		}
	}
	for _, conversation := range conversations {
		if conversation.api.Valid {
			if !validConversationAPI(protocol.APIStyle(conversation.api.String)) {
				return fmt.Errorf("conversation %s has unsupported API %q", conversation.id, conversation.api.String)
			}
			continue
		}
		api, err := determineLegacyAPI(ctx, tx, conversation, modelAPIs)
		if err != nil {
			return err
		}
		archive, err := loadLegacyArchive(ctx, tx, conversation.id)
		if err != nil {
			return err
		}
		archiveRaw := make([]json.RawMessage, len(archive))
		for i, record := range archive {
			archiveRaw[i] = record
		}
		convertedArchive, err := convertLegacyRecords(api, archiveRaw)
		if err != nil {
			return fmt.Errorf("convert conversation %s archive: %w", conversation.id, err)
		}
		if _, err := decodeNativeRecords(api, convertedArchive.records); err != nil {
			return fmt.Errorf("validate conversation %s native archive: %w", conversation.id, err)
		}
		if _, err := tx.ExecContext(ctx, `DELETE FROM sampler_conversation_item WHERE conversation_id=$1`, conversation.id); err != nil {
			return fmt.Errorf("replace conversation %s archive: %w", conversation.id, err)
		}
		for i, record := range convertedArchive.records {
			if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_conversation_item(conversation_id,ordinal,record) VALUES($1,$2,$3)`,
				conversation.id, i+1, []byte(record)); err != nil {
				return fmt.Errorf("write converted conversation %s record %d: %w", conversation.id, i+1, err)
			}
		}

		active := convertedArchive
		if !isNullJSON(conversation.context) {
			var legacyContext []json.RawMessage
			if err := json.Unmarshal(conversation.context, &legacyContext); err != nil {
				return fmt.Errorf("decode conversation %s active context: %w", conversation.id, err)
			}
			active, err = convertLegacyRecords(api, legacyContext)
			if err != nil {
				return fmt.Errorf("convert conversation %s active context: %w", conversation.id, err)
			}
		}
		contextWire, err := nativeContextWire(api, active)
		if err != nil {
			return fmt.Errorf("encode conversation %s active context: %w", conversation.id, err)
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_conversation SET api=$2,context=$3 WHERE id=$1`, conversation.id, string(api), contextWire); err != nil {
			return fmt.Errorf("save conversation %s native state: %w", conversation.id, err)
		}
	}

	if _, err := tx.ExecContext(ctx, `ALTER TABLE sampler_conversation ALTER COLUMN api SET NOT NULL`); err != nil {
		return fmt.Errorf("require conversation API: %w", err)
	}
	var hasAPIConstraint bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
		JOIN pg_namespace n ON n.oid=t.relnamespace
		WHERE n.nspname=current_schema() AND t.relname='sampler_conversation'
		AND c.conname='sampler_conversation_api_check')`).Scan(&hasAPIConstraint); err != nil {
		return fmt.Errorf("inspect conversation API constraint: %w", err)
	}
	if !hasAPIConstraint {
		if _, err := tx.ExecContext(ctx, `ALTER TABLE sampler_conversation ADD CONSTRAINT sampler_conversation_api_check
			CHECK (api IN ('responses','anthropic_messages'))`); err != nil {
			return fmt.Errorf("constrain conversation API: %w", err)
		}
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	return nil
}

func loadLegacyConversations(ctx context.Context, tx *sql.Tx) ([]legacyConversation, error) {
	rows, err := tx.QueryContext(ctx, `SELECT c.id::text,c.api,c.context,s.model,
		COALESCE(c.owner IS NOT NULL AND c.lease_until > clock_timestamp(),FALSE)
		FROM sampler_conversation c LEFT JOIN sampler_session s ON s.conversation_id=c.id ORDER BY c.id FOR UPDATE OF c`)
	if err != nil {
		return nil, fmt.Errorf("read conversations for native migration: %w", err)
	}
	defer rows.Close()
	var conversations []legacyConversation
	for rows.Next() {
		var conversation legacyConversation
		var model sql.NullString
		if err := rows.Scan(&conversation.id, &conversation.api, &conversation.context, &model, &conversation.liveLease); err != nil {
			return nil, err
		}
		conversation.hasSession = model.Valid
		if model.Valid {
			conversation.sessionModel = model.String
		}
		conversations = append(conversations, conversation)
	}
	return conversations, rows.Err()
}

func determineLegacyAPI(ctx context.Context, tx *sql.Tx, conversation legacyConversation, modelAPIs map[string]protocol.APIStyle) (protocol.APIStyle, error) {
	rows, err := tx.QueryContext(ctx, `SELECT request FROM sampler_intent WHERE conversation_id=$1 ORDER BY id`, conversation.id)
	if err != nil {
		return "", fmt.Errorf("read API evidence for conversation %s: %w", conversation.id, err)
	}
	var requestAPIs []protocol.APIStyle
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			rows.Close()
			return "", err
		}
		var request map[string]json.RawMessage
		if err := json.Unmarshal(raw, &request); err != nil {
			rows.Close()
			return "", fmt.Errorf("decode intent request in conversation %s: %w", conversation.id, err)
		}
		apiRaw, present := request["api"]
		if !present {
			continue
		}
		var api protocol.APIStyle
		if err := json.Unmarshal(apiRaw, &api); err != nil || !validConversationAPI(api) {
			rows.Close()
			return "", fmt.Errorf("conversation %s has invalid intent request.api %s", conversation.id, apiRaw)
		}
		requestAPIs = append(requestAPIs, api)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return "", err
	}
	if err := rows.Close(); err != nil {
		return "", err
	}
	if len(requestAPIs) != 0 {
		api := requestAPIs[0]
		for _, candidate := range requestAPIs[1:] {
			if candidate != api {
				return "", fmt.Errorf("conversation %s has conflicting intent API evidence %q and %q", conversation.id, api, candidate)
			}
		}
		return api, nil
	}
	if conversation.hasSession {
		api, ok := modelAPIs[conversation.sessionModel]
		if !ok || !validConversationAPI(api) {
			return "", fmt.Errorf("conversation %s has no intent API evidence and session model %q has no explicit configured API mapping", conversation.id, conversation.sessionModel)
		}
		return api, nil
	}
	// Before API adapters existed, Store.Create was the only path that made a conversation without
	// a Session. Its initial prompt, intents, and every standalone record used the Responses wire
	// format.
	return protocol.APIStyleResponses, nil
}

func loadLegacyArchive(ctx context.Context, tx *sql.Tx, conversationID string) ([]json.RawMessage, error) {
	rows, err := tx.QueryContext(ctx, `SELECT record FROM sampler_conversation_item WHERE conversation_id=$1 ORDER BY ordinal`, conversationID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var records []json.RawMessage
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		records = append(records, append(json.RawMessage(nil), raw...))
	}
	return records, rows.Err()
}

func validConversationAPI(api protocol.APIStyle) bool {
	return api == protocol.APIStyleResponses || api == protocol.APIStyleAnthropicMessages
}

func isNullJSON(raw []byte) bool {
	return len(bytes.TrimSpace(raw)) == 0 || bytes.Equal(bytes.TrimSpace(raw), []byte("null"))
}

func nativeContextWire(api protocol.APIStyle, converted convertedLegacyContext) ([]byte, error) {
	state, err := decodeNativeRecords(api, converted.records)
	if err != nil {
		return nil, err
	}
	if anthropic, ok := state.(*anthropic.Context); ok && anthropic.Messages == nil {
		anthropic.Messages = []json.RawMessage{}
	}
	wire, err := json.Marshal(state)
	if err != nil {
		return nil, err
	}
	if api == protocol.APIStyleAnthropicMessages && converted.pendingEffort != "" {
		var object map[string]json.RawMessage
		if err := json.Unmarshal(wire, &object); err != nil {
			return nil, err
		}
		effort, err := json.Marshal(converted.pendingEffort)
		if err != nil {
			return nil, err
		}
		object["pending_effort"] = effort
		wire, err = json.Marshal(object)
		if err != nil {
			return nil, err
		}
		if _, err := decodeNativeContext(api, wire); err != nil {
			return nil, err
		}
	}
	return wire, nil
}

type legacyToolProjection struct {
	id        string
	name      string
	canonical []byte
}

// convertLegacyRecords preserves native Responses objects verbatim and translates the old mixed
// Anthropic transcript to the message sequence that anthropicInput previously constructed.
func convertLegacyRecords(api protocol.APIStyle, records []json.RawMessage) (convertedLegacyContext, error) {
	converted := convertedLegacyContext{}
	if api == protocol.APIStyleResponses {
		for i, raw := range records {
			kind, err := legacyRecordType(raw)
			if err != nil {
				return convertedLegacyContext{}, fmt.Errorf("record %d: %w", i+1, err)
			}
			if kind == "reasoning_effort" || kind == "reasoning_effort_restore" {
				continue
			}
			if kind == "anthropic_message" {
				return convertedLegacyContext{}, fmt.Errorf("record %d contains an Anthropic envelope in a Responses conversation", i+1)
			}
			converted.records = append(converted.records, append(json.RawMessage(nil), raw...))
		}
		return converted, nil
	}
	if api != protocol.APIStyleAnthropicMessages {
		return convertedLegacyContext{}, fmt.Errorf("unsupported API %q", api)
	}

	expectedCalls := map[string][]legacyToolProjection{}
	matchedCalls := map[string]int{}
	answered := map[string]bool{}
	pendingEffort := ""
	previousRole := ""
	for i, raw := range records {
		fields, err := legacyObject(raw)
		if err != nil {
			return convertedLegacyContext{}, fmt.Errorf("record %d: %w", i+1, err)
		}
		kind := legacyString(fields["type"])
		switch kind {
		case "anthropic_message":
			native := bytes.TrimSpace(fields["content"])
			if len(native) == 0 || bytes.Equal(native, []byte("null")) {
				return convertedLegacyContext{}, fmt.Errorf("record %d has no wrapped native Anthropic message", i+1)
			}
			role, calls, err := legacyAnthropicToolUses(native)
			if err != nil {
				return convertedLegacyContext{}, fmt.Errorf("record %d native Anthropic message: %w", i+1, err)
			}
			if role != "assistant" && len(calls) != 0 {
				return convertedLegacyContext{}, fmt.Errorf("record %d has tool_use blocks on role %q", i+1, role)
			}
			for _, call := range calls {
				expectedCalls[call.id] = append(expectedCalls[call.id], call)
			}
			converted.records = append(converted.records, append(json.RawMessage(nil), native...))
			previousRole = role
		case "function_call":
			projection, err := legacyFunctionCallProjection(fields)
			if err != nil {
				return convertedLegacyContext{}, fmt.Errorf("record %d function_call projection: %w", i+1, err)
			}
			pending := expectedCalls[projection.id]
			matched := matchedCalls[projection.id]
			if matched >= len(pending) {
				return convertedLegacyContext{}, fmt.Errorf("record %d function_call %q has no preceding native tool_use", i+1, projection.id)
			}
			expected := pending[matched]
			if expected.name != projection.name || !bytes.Equal(expected.canonical, projection.canonical) {
				return convertedLegacyContext{}, fmt.Errorf("record %d function_call %q does not match native tool_use name/input", i+1, projection.id)
			}
			matchedCalls[projection.id] = matched + 1
			// This was an execution projection; the native assistant message already carries it.
		case "reasoning_effort":
			effort := legacyString(fields["input"])
			pendingEffort = ""
			control, err := legacyControlMessage(effort)
			if err != nil {
				return convertedLegacyContext{}, fmt.Errorf("record %d effort marker: %w", i+1, err)
			}
			converted.records = append(converted.records, control)
			previousRole = "system"
		case "reasoning_effort_restore":
			effort := legacyString(fields["input"])
			if previousRole == "user" {
				control, err := legacyControlMessage(effort)
				if err != nil {
					return convertedLegacyContext{}, fmt.Errorf("record %d effort restore marker: %w", i+1, err)
				}
				converted.records = append(converted.records, control)
				previousRole = "system"
			} else {
				pendingEffort = effort
			}
		case "message":
			messages, lastRole, err := legacyPromptMessages(fields)
			if err != nil {
				return convertedLegacyContext{}, fmt.Errorf("record %d message conversion: %w", i+1, err)
			}
			converted.records = append(converted.records, messages...)
			previousRole = lastRole
			if lastRole == "user" && pendingEffort != "" {
				control, err := legacyControlMessage(pendingEffort)
				if err != nil {
					return convertedLegacyContext{}, fmt.Errorf("record %d pending effort: %w", i+1, err)
				}
				converted.records = append(converted.records, control)
				pendingEffort = ""
				previousRole = "system"
			}
		case "function_call_output":
			message, err := legacyToolResultMessage(fields, answered)
			if err != nil {
				return convertedLegacyContext{}, fmt.Errorf("record %d function output conversion: %w", i+1, err)
			}
			converted.records = append(converted.records, message)
			previousRole = "user"
		case "execution_state":
			message, err := legacyExecutionStateMessage(fields["content"])
			if err != nil {
				return convertedLegacyContext{}, fmt.Errorf("record %d execution state conversion: %w", i+1, err)
			}
			converted.records = append(converted.records, message)
			previousRole = "user"
		default:
			return convertedLegacyContext{}, fmt.Errorf("record %d has unsupported Anthropic conversation item %q", i+1, kind)
		}
	}
	for id, calls := range expectedCalls {
		if matchedCalls[id] != len(calls) {
			return convertedLegacyContext{}, fmt.Errorf("native tool_use %q has %d of %d matching function_call projections", id, matchedCalls[id], len(calls))
		}
	}
	converted.pendingEffort = pendingEffort
	return converted, nil
}

func legacyRecordType(raw json.RawMessage) (string, error) {
	fields, err := legacyObject(raw)
	if err != nil {
		return "", err
	}
	kind := legacyString(fields["type"])
	if kind == "" {
		return "", errors.New("record has no type")
	}
	return kind, nil
}

func legacyObject(raw json.RawMessage) (map[string]json.RawMessage, error) {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		return nil, err
	}
	if object == nil {
		return nil, errors.New("record must be a JSON object")
	}
	return object, nil
}

func legacyString(raw json.RawMessage) string {
	var value string
	_ = json.Unmarshal(raw, &value)
	return value
}

func legacyAnthropicToolUses(raw json.RawMessage) (string, []legacyToolProjection, error) {
	fields, err := legacyObject(raw)
	if err != nil {
		return "", nil, err
	}
	role := legacyString(fields["role"])
	if role == "" {
		return "", nil, errors.New("native message has no role")
	}
	var blocks []json.RawMessage
	if err := json.Unmarshal(fields["content"], &blocks); err != nil {
		return "", nil, fmt.Errorf("native message content is not an array: %w", err)
	}
	var calls []legacyToolProjection
	for i, rawBlock := range blocks {
		block, err := legacyObject(rawBlock)
		if err != nil {
			return "", nil, fmt.Errorf("content block %d: %w", i+1, err)
		}
		if legacyString(block["type"]) != "tool_use" {
			continue
		}
		id, name := legacyString(block["id"]), legacyString(block["name"])
		if id == "" || name == "" {
			return "", nil, fmt.Errorf("content block %d has incomplete tool identity", i+1)
		}
		canonical, err := canonicalLegacyJSON(block["input"])
		if err != nil {
			return "", nil, fmt.Errorf("content block %d has invalid tool input: %w", i+1, err)
		}
		calls = append(calls, legacyToolProjection{id: id, name: name, canonical: canonical})
	}
	return role, calls, nil
}

func legacyFunctionCallProjection(fields map[string]json.RawMessage) (legacyToolProjection, error) {
	id, name := legacyString(fields["call_id"]), legacyString(fields["name"])
	if id == "" || name == "" {
		return legacyToolProjection{}, errors.New("projection has incomplete call identity")
	}
	var arguments string
	if err := json.Unmarshal(fields["arguments"], &arguments); err != nil {
		return legacyToolProjection{}, errors.New("projection has no string arguments")
	}
	canonical, err := canonicalLegacyJSON(json.RawMessage(arguments))
	if err != nil {
		return legacyToolProjection{}, fmt.Errorf("invalid projection arguments: %w", err)
	}
	return legacyToolProjection{id: id, name: name, canonical: canonical}, nil
}

func canonicalLegacyJSON(raw json.RawMessage) ([]byte, error) {
	if len(raw) == 0 || !json.Valid(raw) {
		return nil, errors.New("value is not valid JSON")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return nil, errors.New("value contains trailing JSON")
	}
	return json.Marshal(value)
}

func legacyControlMessage(effort string) (json.RawMessage, error) {
	return json.Marshal(map[string]any{
		"role": "system", "content": []any{}, "output_config": map[string]any{"effort": effort},
	})
}

func legacyPromptMessages(fields map[string]json.RawMessage) ([]json.RawMessage, string, error) {
	role := legacyString(fields["role"])
	if role == "developer" {
		role = "user"
	}
	if role != "user" && role != "assistant" {
		return nil, "", fmt.Errorf("unsupported role %q", role)
	}
	content := fields["content"]
	var parts []json.RawMessage
	var text string
	if err := json.Unmarshal(content, &text); err == nil {
		part, err := json.Marshal(map[string]any{"type": "input_text", "text": text})
		if err != nil {
			return nil, "", err
		}
		parts = []json.RawMessage{part}
	} else if err := json.Unmarshal(content, &parts); err != nil {
		return nil, "", fmt.Errorf("content is not a text or content-part list: %w", err)
	}
	blocks, err := legacyAnthropicParts(parts)
	if err != nil {
		return nil, "", err
	}
	wire, err := json.Marshal(map[string]any{"role": role, "content": blocks})
	return []json.RawMessage{wire}, role, err
}

func legacyAnthropicParts(parts []json.RawMessage) ([]any, error) {
	blocks := make([]any, 0, len(parts))
	for i, raw := range parts {
		fields, err := legacyObject(raw)
		if err != nil {
			return nil, fmt.Errorf("content part %d: %w", i+1, err)
		}
		switch legacyString(fields["type"]) {
		case "input_text", "output_text", "text":
			blocks = append(blocks, map[string]any{"type": "text", "text": legacyString(fields["text"])})
		case "input_image":
			var imageURL string
			if err := json.Unmarshal(fields["image_url"], &imageURL); err != nil {
				return nil, fmt.Errorf("invalid image URL: %w", err)
			}
			if !strings.HasPrefix(imageURL, "data:") {
				return nil, errors.New("Anthropic image input requires a base64 data URL")
			}
			meta, data, ok := strings.Cut(strings.TrimPrefix(imageURL, "data:"), ",")
			media, base64 := strings.CutSuffix(meta, ";base64")
			if !ok || !base64 || !oneOfMigration(media, "image/png", "image/jpeg", "image/gif", "image/webp") {
				return nil, errors.New("unsupported Anthropic image data URL")
			}
			blocks = append(blocks, map[string]any{"type": "image", "source": map[string]any{"type": "base64", "media_type": media, "data": data}})
		default:
			return nil, fmt.Errorf("unsupported Anthropic input content type %q", legacyString(fields["type"]))
		}
	}
	return blocks, nil
}

func oneOfMigration(value string, choices ...string) bool {
	for _, choice := range choices {
		if value == choice {
			return true
		}
	}
	return false
}

func legacyToolResultMessage(fields map[string]json.RawMessage, answered map[string]bool) (json.RawMessage, error) {
	callID := legacyString(fields["call_id"])
	var rawParts []json.RawMessage
	if err := json.Unmarshal(fields["output"], &rawParts); err != nil {
		return nil, fmt.Errorf("output is not a content-part list: %w", err)
	}
	blocks, err := legacyAnthropicParts(rawParts)
	if err != nil {
		return nil, err
	}
	if answered[callID] {
		encoded, err := json.Marshal(blocks)
		if err != nil {
			return nil, err
		}
		text := "Asynchronous output from exec call " + callID + ":\n" + string(encoded)
		return json.Marshal(map[string]any{"role": "user", "content": []any{map[string]any{"type": "text", "text": text}}})
	}
	answered[callID] = true
	return json.Marshal(map[string]any{"role": "user", "content": []any{map[string]any{"type": "tool_result", "tool_use_id": callID, "content": blocks}}})
}

func legacyExecutionStateMessage(content json.RawMessage) (json.RawMessage, error) {
	text := "Active execution handles: " + string(content)
	return json.Marshal(map[string]any{"role": "user", "content": []any{map[string]any{"type": "text", "text": text}}})
}
