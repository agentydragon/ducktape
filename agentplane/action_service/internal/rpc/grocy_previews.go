package rpc

import (
	"bytes"
	"context"
	"encoding/json"
	"slices"
	"strconv"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/grpcgate"
)

func (s *OperatorService) GetGrocyReferenceSummaries(ctx context.Context, request *operator_action_servicepb.GetGrocyReferenceSummariesRequest) (*operator_action_servicepb.GetGrocyReferenceSummariesResponse, error) {
	id, err := parseRequestID(request.GetRequestId())
	if err != nil {
		return nil, err
	}
	receipt, err := s.store.Get(ctx, id, grpcgate.Admitted(ctx).Principal)
	if err != nil {
		return nil, err
	}
	if receipt.Action.Group != "grocy_sf" {
		return nil, invalidRequest("request has no matching Grocy preview")
	}
	response := &operator_action_servicepb.GetGrocyReferenceSummariesResponse{}
	// After execution, use recorded names and IDs. A current name could refer to a changed or removed object.
	if receipt.State != action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING || s.previews.Grocy == nil {
		return response, nil
	}
	if keys, fields := grocyPreviewReferences(receipt.Action.Name, receipt.Arguments); len(keys) != 0 {
		response.References = s.previews.Grocy.GrocyReferenceSummaries(ctx, keys, fields)
	}
	return response, nil
}

type grocyReferenceFields struct {
	Product       json.RawMessage `json:"product"`
	Location      json.RawMessage `json:"location"`
	QuantityUnit  json.RawMessage `json:"qu"`
	StockUnit     json.RawMessage `json:"stock_qu"`
	PurchaseUnit  json.RawMessage `json:"purchase_qu"`
	ConsumeUnit   json.RawMessage `json:"consume_qu"`
	ParentProduct json.RawMessage `json:"parent_product"`
	ProductGroup  json.RawMessage `json:"product_group"`
	ShoppingList  json.RawMessage `json:"shopping_list"`
	EntryID       json.RawMessage `json:"entry_id"`
	ClearFields   []string        `json:"clear_fields"`
}

const grocyProductPreviewItems = 32

func grocyPreviewReferences(action string, raw json.RawMessage) ([]string, map[string][]string) {
	var args struct {
		grocyReferenceFields
		Items           []grocyReferenceFields `json:"items"`
		Products        []json.RawMessage      `json:"products"`
		Locations       []json.RawMessage      `json:"locations"`
		EntryIDs        []json.RawMessage      `json:"entry_ids"`
		ShoppingItemID  json.RawMessage        `json:"item_id"`
		ShoppingItemIDs []json.RawMessage      `json:"item_ids"`
		ProductID       json.RawMessage        `json:"productId"`
		ProductToKeep   json.RawMessage        `json:"productIdToKeep"`
		ProductToRemove json.RawMessage        `json:"productIdToRemove"`
		FromLocation    json.RawMessage        `json:"from_location"`
		ToLocation      json.RawMessage        `json:"to_location"`
		Entity          string                 `json:"entity"`
		EntityType      string                 `json:"entity_type"`
		ObjectID        json.RawMessage        `json:"objectId"`
		ObjectIDs       []json.RawMessage      `json:"object_ids"`
	}
	if json.Unmarshal(raw, &args) != nil {
		return nil, nil
	}
	limit := 8
	var changedFields map[string][]string
	if action == "products_edit" {
		args.Items = args.Items[:min(len(args.Items), grocyProductPreviewItems)]
		limit = len(args.Items) * 7 // Product itself and at most six proposed linked values per edit.
		changedFields = make(map[string][]string)
	}
	keys := make([]string, 0, limit)
	seen := make(map[string]bool, limit)
	add := func(kind string, values ...json.RawMessage) {
		for _, value := range values {
			if len(keys) == limit {
				return
			}
			var id int64
			// Grocy resolves strings as names, even if every character is a digit.
			if json.Unmarshal(value, &id) != nil || id <= 0 {
				continue
			}
			key := kind + "/" + strconv.FormatInt(id, 10)
			if !seen[key] {
				keys = append(keys, key)
				seen[key] = true
			}
		}
	}
	switch action {
	case "product_delete":
		add("products", args.Product)
	case "products_merge":
		add("products", args.ProductToKeep, args.ProductToRemove)
	case "get_product_stock", "open_product_stock":
		add("products", args.ProductID)
	case "products_create", "products_edit":
		for _, item := range args.Items {
			if action == "products_edit" {
				add("product_edits", item.Product)
				var id int64
				if json.Unmarshal(item.Product, &id) == nil && id > 0 {
					key := "product_edits/" + strconv.FormatInt(id, 10)
					changedFields[key] = grocyChangedProductLinks(changedFields[key], item)
				}
			}
			add("products", item.ParentProduct)
			add("locations", item.Location)
			add("quantity_units", item.StockUnit, item.PurchaseUnit, item.ConsumeUnit)
			add("product_groups", item.ProductGroup)
		}
	case "stock_get":
		add("products", args.Products...)
		add("locations", args.Locations...)
	case "stock_entries_list":
		if (args.Products == nil) == (args.EntryIDs == nil) {
			return nil, nil
		}
		add("products", args.Products...)
		add("stock_entries", args.EntryIDs...)
	case "stock_entry_edit":
		for _, item := range args.Items {
			add("stock_entries", item.EntryID)
			add("locations", item.Location)
		}
	case "stock_add", "stock_consume", "stock_set":
		for _, item := range args.Items {
			add("products", item.Product)
			add("locations", item.Location)
			add("quantity_units", item.QuantityUnit)
		}
	case "stock_transfer":
		add("products", args.Product)
		add("locations", args.FromLocation, args.ToLocation)
		add("quantity_units", args.QuantityUnit)
	case "shopping_list_get", "shopping_list_clear":
		add("shopping_lists", args.ShoppingList)
	case "shopping_list_item_edit":
		add("shopping_list", args.ShoppingItemID)
	case "shopping_list_items_remove":
		add("shopping_list", args.ShoppingItemIDs...)
	case "shopping_list_items_add":
		for _, item := range args.Items {
			add("shopping_lists", item.ShoppingList)
			add("products", item.Product)
		}
	case "entity_delete", "entity_update":
		if grocyNamedEntity(args.Entity) {
			add(args.Entity, args.ObjectID)
		}
	case "entities_get":
		if grocyNamedEntity(args.EntityType) {
			add(args.EntityType, args.ObjectIDs...)
		}
	}
	return keys, changedFields
}

// Keep the same field order as the approval view and union changes for repeated product IDs.
func grocyChangedProductLinks(fields []string, item grocyReferenceFields) []string {
	for _, link := range []struct {
		name      string
		value     json.RawMessage
		clearable bool
	}{
		{"location", item.Location, false},
		{"stock_qu", item.StockUnit, false},
		{"purchase_qu", item.PurchaseUnit, false},
		{"consume_qu", item.ConsumeUnit, false},
		{"parent_product", item.ParentProduct, true},
		{"product_group", item.ProductGroup, true},
	} {
		changed := len(link.value) != 0 && !bytes.Equal(link.value, []byte("null"))
		if (changed || link.clearable && slices.Contains(item.ClearFields, link.name)) && !slices.Contains(fields, link.name) {
			fields = append(fields, link.name)
		}
	}
	return fields
}

func grocyNamedEntity(kind string) bool {
	switch kind {
	case "products", "locations", "quantity_units", "product_groups", "shopping_lists":
		return true
	default:
		return false
	}
}
