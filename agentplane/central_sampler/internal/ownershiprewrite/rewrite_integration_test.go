package ownershiprewrite

import (
	"context"
	"database/sql"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
)

func TestForwardRequiresCompleteAccountManifestAndPreservesRows(t *testing.T) {
	db := newTestDB(t)
	ctx := context.Background()
	first := insertSession(t, db, "account-a", "model-before")
	second := insertSession(t, db, "account-a", "model-before")
	other := insertSession(t, db, "account-b", "other-model")
	setRetainedRelatedData(t, db, first)
	firstBefore := preservedState(t, db, first)
	secondBefore := preservedState(t, db, second)

	if err := Forward(ctx, db, []string{first}, "account-a"); err == nil {
		t.Fatal("Forward accepted an incomplete account manifest")
	}
	assertOwner(t, db, first, "account-a", "")
	assertOwner(t, db, second, "account-a", "")

	if err := Forward(ctx, db, []string{second, first}, "account-a"); err != nil {
		t.Fatalf("Forward: %v", err)
	}
	assertOwner(t, db, first, "", peertls.SessionsURI)
	assertOwner(t, db, second, "", peertls.SessionsURI)
	assertOwner(t, db, other, "account-b", "")
	assertPreservedState(t, db, first, firstBefore)
	assertPreservedState(t, db, second, secondBefore)
	if err := Forward(ctx, db, []string{first, second}, "account-a"); err != nil {
		t.Fatalf("idempotent Forward: %v", err)
	}
	late := insertSession(t, db, "account-a", "created-after-cutover")
	if err := Forward(ctx, db, []string{first, second}, "account-a"); err == nil {
		t.Fatal("Forward accepted a retry after an account-owned Session was created outside the manifest")
	}
	assertOwner(t, db, late, "account-a", "")
}

func TestInventoryReturnsSortedIDsForOnlyTheSuppliedAccount(t *testing.T) {
	db := newTestDB(t)
	ctx := context.Background()
	first := insertSession(t, db, "account-a", "first")
	second := insertSession(t, db, "account-a", "second")
	_ = insertSession(t, db, "account-b", "foreign")
	serviceOwned := insertSession(t, db, "account-a", "service-owned")
	setServiceOwner(t, db, serviceOwned, peertls.SessionsURI)

	ids, err := Inventory(ctx, db, "account-a")
	if err != nil {
		t.Fatalf("Inventory: %v", err)
	}
	want := []string{first, second}
	if want[0] > want[1] {
		want[0], want[1] = want[1], want[0]
	}
	if len(ids) != len(want) || ids[0] != want[0] || ids[1] != want[1] {
		t.Fatalf("Inventory = %v, want sorted IDs %v", ids, want)
	}

	empty, err := Inventory(ctx, db, "account-with-no-sessions")
	if err != nil {
		t.Fatalf("Inventory for empty account: %v", err)
	}
	if empty == nil || len(empty) != 0 {
		t.Fatalf("Inventory for empty account = %#v, want empty JSON-compatible slice", empty)
	}
}

func TestReverseOnlyRestoresManifestIDsAndIsIdempotent(t *testing.T) {
	db := newTestDB(t)
	ctx := context.Background()
	first := insertSession(t, db, "account-a", "first")
	second := insertSession(t, db, "account-a", "second")
	setServiceOwner(t, db, first, peertls.SessionsURI)
	setServiceOwner(t, db, second, peertls.SessionsURI)
	setRetainedRelatedData(t, db, first)
	firstBefore := preservedState(t, db, first)
	secondBefore := preservedState(t, db, second)
	otherService := insertSession(t, db, "account-a", "other-service")
	setServiceOwner(t, db, otherService, "spiffe://agentplane/other")
	otherAccount := insertSession(t, db, "account-b", "other-account")

	if err := Reverse(ctx, db, []string{first, second}, "account-a"); err != nil {
		t.Fatalf("Reverse: %v", err)
	}
	if err := Reverse(ctx, db, []string{first, second}, "account-a"); err != nil {
		t.Fatalf("idempotent Reverse: %v", err)
	}
	assertOwner(t, db, first, "account-a", "")
	assertOwner(t, db, second, "account-a", "")
	assertOwner(t, db, otherService, "", "spiffe://agentplane/other")
	assertOwner(t, db, otherAccount, "account-b", "")
	assertPreservedState(t, db, first, firstBefore)
	assertPreservedState(t, db, second, secondBefore)
}

func TestReverseRejectsMissingAndForeignOwnershipWithoutChangingRows(t *testing.T) {
	db := newTestDB(t)
	ctx := context.Background()
	owned := insertSession(t, db, "account-a", "owned")
	setServiceOwner(t, db, owned, peertls.SessionsURI)
	foreign := insertSession(t, db, "account-b", "foreign")

	if err := Reverse(ctx, db, []string{owned, "missing-session"}, "account-a"); err == nil {
		t.Fatal("Reverse accepted a manifest containing an unknown Session")
	}
	assertOwner(t, db, owned, "", peertls.SessionsURI)
	if err := Reverse(ctx, db, []string{owned, foreign}, "account-a"); err == nil {
		t.Fatal("Reverse accepted a manifest containing another account's Session")
	}
	assertOwner(t, db, owned, "", peertls.SessionsURI)
	assertOwner(t, db, foreign, "account-b", "")
}

func insertSession(t *testing.T, db *sql.DB, accountUID, model string) string {
	t.Helper()
	var id string
	if err := db.QueryRow(`
		INSERT INTO sampler_conversation (id, api) VALUES (gen_random_uuid(), 'responses')
		RETURNING id::text`).Scan(&id); err != nil {
		t.Fatal(err)
	}
	_, err := db.Exec(`
		INSERT INTO sampler_session
			(id, conversation_id, account_uid, configuration, model, effort, source_id)
		VALUES ($1, $2, $3, ''::bytea, $4, 'medium', 'test')`, id, id, accountUID, model)
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func setServiceOwner(t *testing.T, db *sql.DB, id, spiffeID string) {
	t.Helper()
	if _, err := db.Exec(`UPDATE sampler_session SET account_uid = '', service_spiffe_id = $1 WHERE id = $2`, spiffeID, id); err != nil {
		t.Fatal(err)
	}
}

type stateSnapshot struct {
	session      string
	conversation string
	intents      string
}

func setRetainedRelatedData(t *testing.T, db *sql.DB, sessionID string) {
	t.Helper()
	if _, err := db.Exec(`
		UPDATE sampler_conversation c
		SET generation = 7, stopped = TRUE, executor_session = 'retained-upstream-session',
			instructions = 'retained conversation instructions'
		FROM sampler_session s
		WHERE s.conversation_id = c.id AND s.id = $1`, sessionID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`
		INSERT INTO sampler_intent (id, conversation_id, generation, request, result)
		SELECT gen_random_uuid(), conversation_id, 7, '{"kind":"continuation"}'::jsonb,
			'{"status":"pending"}'::jsonb
		FROM sampler_session WHERE id = $1`, sessionID); err != nil {
		t.Fatal(err)
	}
}

func preservedState(t *testing.T, db *sql.DB, sessionID string) stateSnapshot {
	t.Helper()
	var state stateSnapshot
	if err := db.QueryRow(`
		SELECT (to_jsonb(s) - 'account_uid' - 'service_spiffe_id')::text
		FROM sampler_session s WHERE s.id = $1`, sessionID).Scan(&state.session); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`
		SELECT to_jsonb(c)::text
		FROM sampler_conversation c
		JOIN sampler_session s ON s.conversation_id = c.id
		WHERE s.id = $1`, sessionID).Scan(&state.conversation); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`
		SELECT COALESCE(jsonb_agg(to_jsonb(i) ORDER BY i.id), '[]'::jsonb)::text
		FROM sampler_intent i
		JOIN sampler_session s ON s.conversation_id = i.conversation_id
		WHERE s.id = $1`, sessionID).Scan(&state.intents); err != nil {
		t.Fatal(err)
	}
	return state
}

func assertPreservedState(t *testing.T, db *sql.DB, sessionID string, want stateSnapshot) {
	t.Helper()
	got := preservedState(t, db, sessionID)
	if got != want {
		t.Fatalf("ownership rewrite changed Session or related state:\n got: %+v\nwant: %+v", got, want)
	}
}

func assertOwner(t *testing.T, db *sql.DB, id, accountUID, serviceSPIFFEID string) {
	t.Helper()
	var actualAccountUID string
	var actualServiceID sql.NullString
	if err := db.QueryRow(`SELECT account_uid, service_spiffe_id FROM sampler_session WHERE id = $1`, id).Scan(&actualAccountUID, &actualServiceID); err != nil {
		t.Fatal(err)
	}
	if actualAccountUID != accountUID {
		t.Fatalf("Session %s account owner = %q, want %q", id, actualAccountUID, accountUID)
	}
	if serviceSPIFFEID == "" {
		if actualServiceID.Valid {
			t.Fatalf("Session %s service owner = %q, want NULL", id, actualServiceID.String)
		}
		return
	}
	if !actualServiceID.Valid || actualServiceID.String != serviceSPIFFEID {
		t.Fatalf("Session %s service owner = %q, want %q", id, actualServiceID.String, serviceSPIFFEID)
	}
}
