package rpc

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"google.golang.org/grpc/codes"
)

type grocySummariesFunc func(context.Context, []string, map[string][]string) []*operator_action_servicepb.GrocyReferenceSummary

func (f grocySummariesFunc) GrocyReferenceSummaries(ctx context.Context, refs []string, fields map[string][]string) []*operator_action_servicepb.GrocyReferenceSummary {
	return f(ctx, refs, fields)
}

func TestGrocyPreviewsUseOnlyPendingRecordedReferences(t *testing.T) {
	calls := 0
	previews := IntegrationPreviews{Grocy: grocySummariesFunc(func(_ context.Context, refs []string, _ map[string][]string) []*operator_action_servicepb.GrocyReferenceSummary {
		calls++
		if !slices.Equal(refs, []string{"products/11", "products/22"}) {
			t.Errorf("looked up %v", refs)
		}
		return []*operator_action_servicepb.GrocyReferenceSummary{{Key: "products/11", Name: "Coffee"}}
	})}
	namespace := newPolicyNamespace(t)
	s := serveWithPolicyOptions(t, newCatalog(t, submissionCatalog), namespace, namespace.watch(t), nil, previews)
	request := s.submitAction(t, "grocy_sf", "products_merge", `{"productIdToKeep":11,"productIdToRemove":22,"product":33}`)
	input := &operator_action_servicepb.GetGrocyReferenceSummariesRequest{RequestId: request.ID.String()}
	_, err := s.operators.GetGrocyReferenceSummaries(as(t, callerCertificate), input)
	wantCode(t, "workload cannot read Grocy summaries", err, codes.Unauthenticated)
	got, err := s.operators.GetGrocyReferenceSummaries(as(t, s.operatorToken), input)
	if err != nil || len(got.GetReferences()) != 1 || got.GetReferences()[0].GetName() != "Coffee" {
		t.Fatalf("pending lookup: %v, %v", got, err)
	}
	if _, err := s.store.Cancel(t.Context(), request.ID, s.caller); err != nil {
		t.Fatal(err)
	}
	got, err = s.operators.GetGrocyReferenceSummaries(as(t, s.operatorToken), input)
	if err != nil || len(got.GetReferences()) != 0 || calls != 1 {
		t.Fatalf("cancelled lookup: %v, %v, provider calls %d", got, err, calls)
	}
}

func TestGrocyPreviewReferenceBounds(t *testing.T) {
	got, _ := grocyPreviewReferences("stock_get", json.RawMessage(`{
		"products":[1,"2",1,3,4,5],"locations":[1,2,3,4,5],"entry_ids":[88]
	}`))
	want := []string{"products/1", "products/3", "products/4", "products/5", "locations/1", "locations/2", "locations/3", "locations/4"}
	if !slices.Equal(got, want) {
		t.Errorf("references %v, want %v", got, want)
	}
	if got, _ := grocyPreviewReferences("shopping_list_items_remove", json.RawMessage(`{"item_ids":[52,52,53,"54"],"product":99}`)); !slices.Equal(got, []string{"shopping_list/52", "shopping_list/53"}) {
		t.Errorf("shopping item references %v", got)
	}
	if got, _ := grocyPreviewReferences("products_edit", json.RawMessage(`{"items":[{"product":2,"location":4},{"product":"2"}]}`)); !slices.Equal(got, []string{"product_edits/2", "locations/4"}) {
		t.Errorf("edited product references %v", got)
	}
	if got, _ := grocyPreviewReferences("entity_delete", json.RawMessage(`{"entity":"api_keys","objectId":5}`)); len(got) != 0 {
		t.Errorf("sensitive record references %v", got)
	}
}

func TestGrocyProductPreviewPlansTwentyItems(t *testing.T) {
	items := make([]map[string]any, 0, 20)
	for id := 1; id <= 20; id++ {
		items = append(items, map[string]any{"product": id, "location": "Pantry", "stock_qu": nil})
	}
	items[0]["clear_fields"] = []string{"product_group", "description", "location"}
	raw, err := json.Marshal(map[string]any{"items": items})
	if err != nil {
		t.Fatal(err)
	}
	keys, fields := grocyPreviewReferences("products_edit", raw)
	if len(keys) != 20 || len(fields) != 20 {
		t.Fatalf("expected 20 product reads, got %v, %v", keys, fields)
	}
	for id := 1; id <= 20; id++ {
		key := fmt.Sprintf("product_edits/%d", id)
		want := []string{"location"}
		if id == 1 {
			want = append(want, "product_group")
		}
		if keys[id-1] != key || !slices.Equal(fields[key], want) {
			t.Errorf("row %d: keys=%v, fields=%v, want %v", id, keys, fields[key], want)
		}
	}
}

func TestGrocyRepeatedProductPreviewUnionsChangedLinks(t *testing.T) {
	keys, fields := grocyPreviewReferences("products_edit", json.RawMessage(`{
		"items":[
			{"product":2,"clear_fields":["location","parent_product"],"stock_qu":null},
			{"product":2,"location":4,"name":"Updated"},
			{"product":"2","product_group":"Supplies"}
		]
	}`))
	if !slices.Equal(keys, []string{"product_edits/2", "locations/4"}) ||
		len(fields) != 1 || !slices.Equal(fields["product_edits/2"], []string{"parent_product", "location"}) {
		t.Errorf("unexpected exact references or field list: %v, %v", keys, fields)
	}
}
