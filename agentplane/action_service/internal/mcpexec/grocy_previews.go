package mcpexec

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func (e *Executor) GrocyReferenceSummaries(ctx context.Context, keys []string) []*operator_action_servicepb.GrocyReferenceSummary {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	var entityKeys, entryKeys []string
	for _, key := range keys {
		if strings.HasPrefix(key, "stock_entries/") {
			entryKeys = append(entryKeys, key)
		} else {
			entityKeys = append(entityKeys, key)
		}
	}
	var entities, entries []*operator_action_servicepb.GrocyReferenceSummary
	var reads sync.WaitGroup
	if len(entityKeys) > 0 {
		reads.Go(func() {
			entities = readSummaries(ctx, e, entityKeys, "grocy_sf", "entities_get", func(key string) map[string]any {
				kind, id := grocyReference(key)
				return map[string]any{"entity_type": kind, "object_ids": []int64{id}}
			}, grocyEntitySummary)
		})
	}
	if len(entryKeys) > 0 {
		reads.Go(func() {
			entries = readSummaries(ctx, e, entryKeys, "grocy_sf", "stock_entries_list", func(key string) map[string]any {
				_, id := grocyReference(key)
				return map[string]any{"entry_ids": []int64{id}}
			}, grocyStockEntrySummary)
		})
	}
	reads.Wait()
	return append(entities, entries...)
}

func grocyReference(key string) (string, int64) {
	kind, rawID, _ := strings.Cut(key, "/")
	id, _ := strconv.ParseInt(rawID, 10, 64) // The operator handler builds keys from exact positive integer IDs.
	return kind, id
}

func grocyEntitySummary(key string, result *mcp.CallToolResult) *operator_action_servicepb.GrocyReferenceSummary {
	kind, id := grocyReference(key)
	var rows []struct {
		Kind       string
		EntityType string `json:"entity_type"`
		ObjectID   int64  `json:"object_id"`
		Data       struct{ Name string }
	}
	if !parseGrocyRows(result, &rows) || len(rows) != 1 {
		return nil
	}
	row := rows[0]
	if row.Kind != "ok" || row.EntityType != kind || row.ObjectID != id || strings.TrimSpace(row.Data.Name) == "" {
		return nil
	}
	return &operator_action_servicepb.GrocyReferenceSummary{Key: key, Name: row.Data.Name}
}

func grocyStockEntrySummary(key string, result *mcp.CallToolResult) *operator_action_servicepb.GrocyReferenceSummary {
	_, id := grocyReference(key)
	var rows []struct {
		Kind  string
		Entry struct {
			EntryID     int64  `json:"entry_id"`
			ProductName string `json:"product_name"`
			Location    string `json:"location_name"`
		}
	}
	if !parseGrocyRows(result, &rows) || len(rows) != 1 {
		return nil
	}
	row := rows[0]
	if row.Kind != "ok" || row.Entry.EntryID != id || strings.TrimSpace(row.Entry.ProductName) == "" {
		return nil
	}
	name := row.Entry.ProductName
	if strings.TrimSpace(row.Entry.Location) != "" {
		name += " · " + row.Entry.Location
	}
	return &operator_action_servicepb.GrocyReferenceSummary{Key: key, Name: name}
}

func parseGrocyRows(result *mcp.CallToolResult, rows any) bool {
	if result.StructuredContent != nil {
		payload, err := json.Marshal(result.StructuredContent)
		if err != nil {
			return false
		}
		var envelope struct{ Result json.RawMessage }
		return json.Unmarshal(payload, &envelope) == nil && json.Unmarshal(envelope.Result, rows) == nil
	}
	if len(result.Content) != 1 {
		return false
	}
	text, ok := result.Content[0].(*mcp.TextContent)
	return ok && json.Unmarshal([]byte(text.Text), rows) == nil
}
