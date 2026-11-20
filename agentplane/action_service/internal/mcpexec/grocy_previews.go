package mcpexec

import (
	"context"
	"encoding/json"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type grocyEntityPreview struct {
	summary *operator_action_servicepb.GrocyReferenceSummary
	links   []string
}

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
	var entries []*operator_action_servicepb.GrocyReferenceSummary
	var reads sync.WaitGroup
	reads.Go(func() {
		entries = readSummaries(ctx, e, entryKeys, "grocy_sf", "stock_entries_list", func(key string) map[string]any {
			_, id := grocyReference(key)
			return map[string]any{"entry_ids": []int64{id}}
		}, grocyStockEntrySummary)
	})
	entities := grocyEntities(ctx, e, entityKeys)
	reads.Wait()
	return append(grocyRelatedSummaries(ctx, e, entities), entries...)
}

func grocyEntities(ctx context.Context, e *Executor, keys []string) []*grocyEntityPreview {
	return readSummaries(ctx, e, keys, "grocy_sf", "entities_get", func(key string) map[string]any {
		kind, id := grocyReference(key)
		if kind == "product_edits" {
			kind = "products"
		}
		return map[string]any{"entity_type": kind, "object_ids": []int64{id}}
	}, grocyEntitySummary)
}

// Follow only the exact IDs in the verified result of the first read. Share the original deadline
// and keep a separate total bound on these optional display-name reads.
func grocyRelatedSummaries(ctx context.Context, e *Executor, entities []*grocyEntityPreview) []*operator_action_servicepb.GrocyReferenceSummary {
	const limit = 8
	names := make(map[string]string)
	var summaries []*operator_action_servicepb.GrocyReferenceSummary
	for _, entity := range entities {
		summary := entity.summary
		summaries = append(summaries, summary)
		if summary.Name != "" {
			names[summary.Key] = summary.Name
		}
	}
	var keys []string
	for _, entity := range entities {
		for _, key := range entity.links {
			if _, seen := names[key]; key != "" && !seen && len(keys) < limit {
				names[key] = ""
				keys = append(keys, key)
			}
		}
	}
	for _, related := range grocyEntities(ctx, e, keys) {
		summaries = append(summaries, related.summary)
		names[related.summary.Key] = related.summary.Name
	}
	for _, entity := range entities {
		if item := entity.summary.ShoppingItemCurrent; item != nil {
			if name := names[entity.links[0]]; name != "" {
				entity.summary.Name = name
			}
			item.ListName = names[entity.links[1]]
			item.UnitName = names[entity.links[2]]
		}
	}
	return summaries
}

func grocyReference(key string) (string, int64) {
	kind, rawID, _ := strings.Cut(key, "/")
	id, _ := strconv.ParseInt(rawID, 10, 64) // Keys always carry exact positive integer IDs.
	return kind, id
}

func grocyEntitySummary(key string, result *mcp.CallToolResult) *grocyEntityPreview {
	kind, id := grocyReference(key)
	entityType := kind
	if kind == "product_edits" {
		entityType = "products"
	}
	var rows []struct {
		Kind       string
		EntityType string `json:"entity_type"`
		ObjectID   int64  `json:"object_id"`
		Data       map[string]json.RawMessage
	}
	if !parseGrocyRows(result, &rows) || len(rows) != 1 {
		return nil
	}
	row := rows[0]
	if row.Kind != "ok" || row.EntityType != entityType || row.ObjectID != id || row.Data == nil {
		return nil
	}
	name := row.Data["name"]
	summary := &operator_action_servicepb.GrocyReferenceSummary{Key: key}
	if value := grocyString(name); value != nil {
		summary.Name = *value
	}
	preview := &grocyEntityPreview{summary: summary}
	switch kind {
	case "shopping_list":
		preview.links = []string{
			grocyLinkedKey("products", grocyInt(row.Data["product_id"])),
			grocyLinkedKey("shopping_lists", grocyInt(row.Data["shopping_list_id"])),
			grocyLinkedKey("quantity_units", grocyInt(row.Data["qu_id"])),
		}
		item := &operator_action_servicepb.GrocyShoppingItemCurrent{
			Amount: grocyNumber(row.Data["amount"]),
			Note:   grocyString(row.Data["note"]),
		}
		if done := grocyInt(row.Data["done"]); done != nil && (*done == 0 || *done == 1) {
			value := *done == 1
			item.Done = &value
		}
		if item.Note != nil && preview.links[0] == "" {
			summary.Name = *item.Note
		}
		summary.ShoppingItemCurrent = item
	case "product_edits":
		product := &operator_action_servicepb.GrocyProductEditCurrent{
			LocationId:      grocyInt(row.Data["location_id"]),
			StockUnitId:     grocyInt(row.Data["qu_id_stock"]),
			PurchaseUnitId:  grocyInt(row.Data["qu_id_purchase"]),
			ConsumeUnitId:   grocyInt(row.Data["qu_id_consume"]),
			MinimumStock:    grocyNumber(row.Data["min_stock_amount"]),
			ShelfLifeDays:   grocyInt(row.Data["default_best_before_days"]),
			DueType:         grocyInt(row.Data["due_type"]),
			ParentProductId: grocyInt(row.Data["parent_product_id"]),
			ProductGroupId:  grocyInt(row.Data["product_group_id"]),
			Description:     grocyString(row.Data["description"]),
			Calories:        grocyNumber(row.Data["calories"]),
		}
		summary.ProductEditCurrent = product
		preview.links = []string{
			grocyLinkedKey("locations", product.LocationId),
			grocyLinkedKey("quantity_units", product.StockUnitId),
			grocyLinkedKey("quantity_units", product.PurchaseUnitId),
			grocyLinkedKey("quantity_units", product.ConsumeUnitId),
			grocyLinkedKey("products", product.ParentProductId),
			grocyLinkedKey("product_groups", product.ProductGroupId),
		}
	default:
		if strings.TrimSpace(summary.Name) == "" {
			return nil
		}
	}
	return preview
}

func grocyLinkedKey(kind string, id *int64) string {
	if id == nil || *id <= 0 {
		return ""
	}
	return kind + "/" + strconv.FormatInt(*id, 10)
}

func grocyString(raw json.RawMessage) *string {
	var value *string
	if json.Unmarshal(raw, &value) != nil {
		return nil
	}
	return value
}

func grocyInt(raw json.RawMessage) *int64 {
	if string(raw) == "true" {
		value := int64(1)
		return &value
	}
	if string(raw) == "false" {
		value := int64(0)
		return &value
	}
	value, err := strconv.ParseInt(strings.Trim(string(raw), `"`), 10, 64)
	if err != nil {
		return nil
	}
	return &value
}

func grocyNumber(raw json.RawMessage) *float64 {
	value, err := strconv.ParseFloat(strings.Trim(string(raw), `"`), 64)
	if err != nil || math.IsNaN(value) || math.IsInf(value, 0) {
		return nil
	}
	return &value
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
