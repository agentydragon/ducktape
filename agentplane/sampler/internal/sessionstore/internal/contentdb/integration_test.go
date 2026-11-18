package contentdb

import (
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
)

func TestContentAndContextShareHashesAndFollowCallerRollback(t *testing.T) {
	db := openTestDB(t)
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	store := New(t.Context(), tx, providerrequest.ContentReferences)

	instructions, err := store.PutContent(content.ContentText, json.RawMessage(`"fixture instructions"`))
	if err != nil {
		t.Fatal(err)
	}
	tool, err := store.PutContent(responses.ContentOpenAITool, json.RawMessage(`{"name":"fixture_tool"}`))
	if err != nil {
		t.Fatal(err)
	}
	profilePayload, err := json.Marshal(map[string]any{
		"instructions_ref": instructions,
		"tool_refs":        []content.ContentID{tool},
	})
	if err != nil {
		t.Fatal(err)
	}
	profile, err := store.PutContent(responses.ContentOpenAIRequest, profilePayload)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Save(); err != nil {
		t.Fatal(err)
	}
	reader := New(t.Context(), tx, providerrequest.ContentReferences)
	if err := reader.Prefetch([]content.ContentID{profile}); err != nil {
		t.Fatalf("prefetch provider references: %v", err)
	}
	for _, id := range []content.ContentID{instructions, tool} {
		if _, err := reader.GetContent(id); err != nil {
			t.Fatalf("load referenced content %s: %v", id, err)
		}
	}

	firstRoot, err := store.StoreContext(protocol.APIStyleResponses, []content.ContentID{instructions, tool})
	if err != nil {
		t.Fatal(err)
	}
	extra, err := store.PutContent(content.ContentText, json.RawMessage(`"later item"`))
	if err != nil {
		t.Fatal(err)
	}
	nextRoot, err := store.AppendContextIDs(protocol.APIStyleResponses, firstRoot, 2, []content.ContentID{extra})
	if err != nil {
		t.Fatal(err)
	}
	got, err := store.LoadContext(nextRoot)
	if err != nil {
		t.Fatal(err)
	}
	if want := []content.ContentID{instructions, tool, extra}; !reflect.DeepEqual(got, want) {
		t.Fatalf("LoadContext() = %v, want %v", got, want)
	}
	if extends, err := store.ContextExtends(nextRoot, firstRoot); err != nil || !extends {
		t.Fatalf("ContextExtends(next, first) = %t, %v; want true", extends, err)
	}
	if extends, err := store.ContextExtends(firstRoot, nextRoot); err != nil || extends {
		t.Fatalf("ContextExtends(first, next) = %t, %v; want false", extends, err)
	}
	if count, err := store.ContextItemCount(nextRoot); err != nil || count != 3 {
		t.Fatalf("ContextItemCount(next) = %d, %v; want 3", count, err)
	}

	missingPayload := json.RawMessage(`{"instructions_ref":"missing-content"}`)
	missingRoot, err := store.PutContent(responses.ContentOpenAIRequest, missingPayload)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Save(); err != nil {
		t.Fatal(err)
	}
	if err := store.Prefetch([]content.ContentID{missingRoot}); err == nil || !strings.Contains(err.Error(), "missing provider content") {
		t.Fatalf("Prefetch(missing reference) error = %v, want missing provider content", err)
	}

	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	for _, table := range []string{"sampler_content", "sampler_context"} {
		var count int
		if err := db.QueryRowContext(t.Context(), `SELECT COUNT(*) FROM `+table).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 0 {
			t.Fatalf("%s rows after caller rollback = %d, want 0", table, count)
		}
	}
}

func TestGetContentVerifiesStoredHash(t *testing.T) {
	db := openTestDB(t)
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	store := New(t.Context(), tx, providerrequest.ContentReferences)
	id, err := store.PutContent(content.ContentText, json.RawMessage(`"original"`))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Save(); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.ExecContext(t.Context(), `UPDATE sampler_content SET payload=$2 WHERE id=$1`, id, []byte(`"changed"`)); err != nil {
		t.Fatal(err)
	}
	reader := New(t.Context(), tx, providerrequest.ContentReferences)
	if _, err := reader.GetContent(id); err == nil || !strings.Contains(err.Error(), "content hash mismatch") {
		t.Fatalf("GetContent() error = %v, want content hash mismatch", err)
	}
}

func TestPrefetchHandlesMoreThanOneBoundedBatch(t *testing.T) {
	db := openTestDB(t)
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	store := New(t.Context(), tx, providerrequest.ContentReferences)
	ids := make([]content.ContentID, 257)
	for index := range ids {
		id, err := store.PutContent(content.ContentText, json.RawMessage(fmt.Sprintf(`"value-%03d"`, index)))
		if err != nil {
			t.Fatal(err)
		}
		ids[index] = id
	}
	if err := store.Save(); err != nil {
		t.Fatal(err)
	}
	reader := New(t.Context(), tx, providerrequest.ContentReferences)
	if err := reader.Prefetch(ids); err != nil {
		t.Fatalf("Prefetch(%d roots): %v", len(ids), err)
	}
}
