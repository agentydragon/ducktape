package mcpexec

import (
	"context"
	"encoding/json"
	"math"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func (e *Executor) GrocyReferenceSummaries(ctx context.Context, keys []string, changedFields map[string][]string) []*operator_action_servicepb.GrocyReferenceSummary {
	if e == nil || e.key != "grocy_sf" || len(keys) == 0 {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	s := e.pin()
	if s == nil {
		return nil
	}
	defer s.calls.Done()
	tools, _, err := e.list(ctx, s)
	if err != nil {
		return nil
	}
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
		entries = readOfferedSummaries(ctx, e, s, tools, entryKeys, "stock_entries_list", func(key string) map[string]any {
			_, id := grocyReference(key)
			return map[string]any{"entry_ids": []int64{id}}
		}, grocyStockEntrySummary)
	})
	entities := grocyEntities(ctx, e, s, tools, entityKeys)
	reads.Wait()

	// Derive old references only from the exact product rows and fields the Action changes.
	seen := make(map[string]bool)
	for _, summary := range entities {
		seen[summary.Key] = true
	}
	var linkedKeys []string
	for _, summary := range entities {
		for _, field := range changedFields[summary.Key] {
			key := grocyProductLink(summary.GetProductEditCurrent(), field)
			if key != "" && !seen[key] {
				seen[key] = true
				linkedKeys = append(linkedKeys, key)
			}
		}
	}
	return append(append(entities, grocyEntities(ctx, e, s, tools, linkedKeys)...), entries...)
}

func grocyProductLink(product *operator_action_servicepb.GrocyProductEditCurrent, field string) string {
	if product == nil {
		return ""
	}
	switch field {
	case "location":
		return grocyLinkedKey("locations", product.LocationId)
	case "stock_qu":
		return grocyLinkedKey("quantity_units", product.StockUnitId)
	case "purchase_qu":
		return grocyLinkedKey("quantity_units", product.PurchaseUnitId)
	case "consume_qu":
		return grocyLinkedKey("quantity_units", product.ConsumeUnitId)
	case "parent_product":
		return grocyLinkedKey("products", product.ParentProductId)
	case "product_group":
		return grocyLinkedKey("product_groups", product.ProductGroupId)
	}
	return ""
}

type grocyEntityBatch struct {
	kind string
	keys []string
}

func grocyEntityType(kind string) string {
	if kind == "product_edits" {
		return "products"
	}
	return kind
}

func grocyEntities(ctx context.Context, e *Executor, s *session, tools map[string]tool, keys []string) []*operator_action_servicepb.GrocyReferenceSummary {
	byType := make(map[string][]string)
	for _, key := range keys {
		kind, _ := grocyReference(key)
		kind = grocyEntityType(kind)
		byType[kind] = append(byType[kind], key)
	}
	var batches []grocyEntityBatch
	for kind, refs := range byType {
		for chunk := range slices.Chunk(refs, 32) {
			batches = append(batches, grocyEntityBatch{kind: kind, keys: chunk})
		}
	}
	responses := readOfferedSummaries(ctx, e, s, tools, batches, "entities_get", func(batch grocyEntityBatch) map[string]any {
		ids := make([]int64, 0, len(batch.keys))
		for _, key := range batch.keys {
			_, id := grocyReference(key)
			if !slices.Contains(ids, id) {
				ids = append(ids, id)
			}
		}
		return map[string]any{"entity_type": batch.kind, "object_ids": ids}
	}, func(batch grocyEntityBatch, result *mcp.CallToolResult) *[]*operator_action_servicepb.GrocyReferenceSummary {
		summaries := grocyEntitySummaries(batch.keys, result)
		return &summaries
	})
	byKey := make(map[string]*operator_action_servicepb.GrocyReferenceSummary)
	for _, response := range responses {
		for _, summary := range *response {
			byKey[summary.Key] = summary
		}
	}
	var summaries []*operator_action_servicepb.GrocyReferenceSummary
	for _, key := range keys {
		if summary := byKey[key]; summary != nil {
			summaries = append(summaries, summary)
		}
	}
	return summaries
}

func grocyReference(key string) (string, int64) {
	kind, rawID, _ := strings.Cut(key, "/")
	id, _ := strconv.ParseInt(rawID, 10, 64) // Keys always carry exact positive integer IDs.
	return kind, id
}

type grocyEntityRow struct {
	Kind              string
	EntityType        string `json:"entity_type"`
	ObjectID          int64  `json:"object_id"`
	Data              map[string]json.RawMessage
	ShoppingItemNames struct {
		ProductName string `json:"product_name"`
		ListName    string `json:"list_name"`
		UnitName    string `json:"unit_name"`
	} `json:"shopping_item_names"`
}

func grocyEntitySummaries(keys []string, result *mcp.CallToolResult) []*operator_action_servicepb.GrocyReferenceSummary {
	var rows []grocyEntityRow
	if !parseGrocyRows(result, &rows) {
		return nil
	}
	byKey := make(map[string]*grocyEntityRow)
	for i, row := range rows {
		key := row.EntityType + "/" + strconv.FormatInt(row.ObjectID, 10)
		if _, duplicate := byKey[key]; duplicate {
			byKey[key] = nil
		} else {
			byKey[key] = &rows[i]
		}
	}
	var summaries []*operator_action_servicepb.GrocyReferenceSummary
	for _, key := range keys {
		kind, id := grocyReference(key)
		row := byKey[grocyEntityType(kind)+"/"+strconv.FormatInt(id, 10)]
		if row != nil && row.Kind == "ok" && row.Data != nil {
			if summary := grocySummaryFromRow(key, kind, row); summary != nil {
				summaries = append(summaries, summary)
			}
		}
	}
	return summaries
}

func grocySummaryFromRow(key, kind string, row *grocyEntityRow) *operator_action_servicepb.GrocyReferenceSummary {
	name := row.Data["name"]
	summary := &operator_action_servicepb.GrocyReferenceSummary{Key: key}
	if value := grocyString(name); value != nil {
		summary.Name = *value
	}
	switch kind {
	case "shopping_list":
		names := row.ShoppingItemNames
		item := &operator_action_servicepb.GrocyShoppingItemCurrent{
			Amount:   grocyNumber(row.Data["amount"]),
			Note:     grocyString(row.Data["note"]),
			ListName: names.ListName,
			UnitName: names.UnitName,
		}
		if done := grocyInt(row.Data["done"]); done != nil && (*done == 0 || *done == 1) {
			value := *done == 1
			item.Done = &value
		}
		if names.ProductName != "" {
			summary.Name = names.ProductName
		} else if productID := grocyInt(row.Data["product_id"]); item.Note != nil && (productID == nil || *productID <= 0) {
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
	default:
		if strings.TrimSpace(summary.Name) == "" {
			return nil
		}
	}
	return summary
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
