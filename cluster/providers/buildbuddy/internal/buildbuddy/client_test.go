package buildbuddy

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"golang.org/x/crypto/nacl/box"
)

func TestSecretLifecycle(t *testing.T) {
	public, private, err := box.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	var stored []byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-buildbuddy-api-key") != "fake-admin" {
			t.Error("missing API key")
		}
		switch strings.TrimPrefix(r.URL.Path, "/rpc/") {
		case "GetPublicKey":
			_ = json.NewEncoder(w).Encode(map[string]any{"publicKey": map[string]string{"id": "fake-key", "value": base64.StdEncoding.EncodeToString(public[:])}})
		case "ListSecrets":
			var secrets []map[string]string
			if stored != nil {
				secrets = append(secrets, map[string]string{"name": "FAKE_TEST_KEY"})
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"secret": secrets})
		case "UpdateSecret":
			var request struct {
				Secret struct{ Name, Value string } `json:"secret"`
			}
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
				t.Error(err)
				return
			}
			if request.Secret.Name != "FAKE_TEST_KEY" {
				t.Errorf("unexpected name %s", request.Secret.Name)
			}
			sealed, err := base64.StdEncoding.DecodeString(request.Secret.Value)
			if err != nil {
				t.Error(err)
				return
			}
			var ok bool
			stored, ok = box.OpenAnonymous(nil, sealed, public, private)
			if !ok {
				t.Error("cannot decrypt sealed value")
			}
		case "DeleteSecret":
			stored = nil
		default:
			http.Error(w, "unknown method", http.StatusNotFound)
		}
	}))
	defer server.Close()
	c := Client{URL: server.URL + "/rpc/", APIKey: "fake-admin", HTTP: server.Client()}
	ctx := context.Background()
	key, err := c.PublicKey(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Put(ctx, "FAKE_TEST_KEY", []byte("fake-value"), key); err != nil {
		t.Fatal(err)
	}
	if string(stored) != "fake-value" {
		t.Fatal("server received wrong value")
	}
	if exists, err := c.Exists(ctx, "FAKE_TEST_KEY"); err != nil || !exists {
		t.Fatalf("exists=%v err=%v", exists, err)
	}
	if err := c.Delete(ctx, "FAKE_TEST_KEY"); err != nil {
		t.Fatal(err)
	}
	if exists, err := c.Exists(ctx, "FAKE_TEST_KEY"); err != nil || exists {
		t.Fatalf("exists=%v err=%v", exists, err)
	}
}

func TestRejectsInvalidPublicKey(t *testing.T) {
	c := Client{}
	if err := c.Put(context.Background(), "FAKE", []byte("fake"), PublicKey{Value: "bad"}); err == nil {
		t.Fatal("expected invalid public key")
	}
}
