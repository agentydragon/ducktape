package rpc

import (
	"context"
	"encoding/json"
	"slices"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
)

type grocySummariesFunc func(context.Context, []string) []*operator_action_servicepb.GrocyReferenceSummary

func (f grocySummariesFunc) GrocyReferenceSummaries(ctx context.Context, refs []string) []*operator_action_servicepb.GrocyReferenceSummary {
	return f(ctx, refs)
}

func TestGrocyPreviewsUseOnlyPendingRecordedReferences(t *testing.T) {
	calls := 0
	previews := IntegrationPreviews{Grocy: grocySummariesFunc(func(_ context.Context, refs []string) []*operator_action_servicepb.GrocyReferenceSummary {
		calls++
		if !slices.Equal(refs, []string{"products/11", "products/22"}) {
			t.Errorf("looked up %v", refs)
		}
		return []*operator_action_servicepb.GrocyReferenceSummary{{Key: "products/11", Name: "Coffee"}}
	})}
	namespace := newPolicyNamespace(t)
	s := serveWithPolicyOptions(t, newCatalog(t, submissionCatalog), namespace, namespace.watch(t), nil, previews)
	request, err := s.store.Submit(t.Context(), s.caller, store.ActionRequestInput{
		IdempotencyKey: "grocy-previews", Action: store.ActionIdentity{Group: "grocy_sf", Name: "products_merge"},
		Arguments: json.RawMessage(`{"productIdToKeep":11,"productIdToRemove":22,"product":33}`), Title: "Merge coffee",
	}, store.Admission{RequestID: uuid.New()})
	if err != nil {
		t.Fatal(err)
	}
	input := &operator_action_servicepb.GetGrocyReferenceSummariesRequest{RequestId: request.ID.String()}
	_, err = s.operators.GetGrocyReferenceSummaries(as(t, callerCertificate), input)
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
	got := grocyPreviewReferences("stock_get", json.RawMessage(`{
		"products":[1,"2",1,3,4,5],"locations":[1,2,3,4,5],"entry_ids":[88]
	}`))
	want := []string{"products/1", "products/3", "products/4", "products/5", "locations/1", "locations/2", "locations/3", "locations/4"}
	if !slices.Equal(got, want) {
		t.Errorf("references %v, want %v", got, want)
	}
	if got := grocyPreviewReferences("entity_delete", json.RawMessage(`{"entity":"api_keys","objectId":5}`)); len(got) != 0 {
		t.Errorf("sensitive record references %v", got)
	}
}
