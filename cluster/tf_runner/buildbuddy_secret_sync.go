// buildbuddy-secret-sync manages the LiteLLM credential exposed to BuildBuddy
// test actions. BuildBuddy stores sealed-box ciphertext; this binary never
// writes the plaintext token or ciphertext to logs.
package main

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"golang.org/x/crypto/nacl/box"
)

const (
	apiURL        = "https://app.buildbuddy.io/rpc/BuildBuddyService/"
	secretName    = "OPENAI_API_KEY"
	adminKeyEnv   = "BUILDBUDDY_API_KEY"
	litellmKeyEnv = "LITELLM_API_KEY"
)

type publicKey struct {
	ID    string `json:"id"`
	Value string `json:"value"`
}

func main() {
	if len(os.Args) != 2 {
		fatal("expected one command: public-key-id, set, or delete")
	}

	var err error
	switch os.Args[1] {
	case "public-key-id":
		var key publicKey
		key, err = getPublicKey()
		if err == nil {
			err = json.NewEncoder(os.Stdout).Encode(map[string]string{"key_id": key.ID})
		}
	case "set":
		err = setSecret()
	case "delete":
		err = deleteSecret()
	default:
		fatal("unknown command")
	}
	if err != nil {
		fatal(err.Error())
	}
}

func getPublicKey() (publicKey, error) {
	var response struct {
		PublicKey publicKey `json:"publicKey"`
	}
	if err := request("GetPublicKey", map[string]any{}, &response); err != nil {
		return publicKey{}, err
	}
	if response.PublicKey.ID == "" || response.PublicKey.Value == "" {
		return publicKey{}, errors.New("BuildBuddy returned an incomplete public key")
	}
	return response.PublicKey, nil
}

func setSecret() error {
	value := os.Getenv(litellmKeyEnv)
	if value == "" {
		return fmt.Errorf("%s is required", litellmKeyEnv)
	}
	key, err := getPublicKey()
	if err != nil {
		return err
	}
	publicKeyBytes, err := base64.StdEncoding.DecodeString(key.Value)
	var publicKeyArray [32]byte
	if err != nil || len(publicKeyBytes) != len(publicKeyArray) {
		return errors.New("BuildBuddy returned an invalid public key")
	}
	copy(publicKeyArray[:], publicKeyBytes)
	ciphertext, err := box.SealAnonymous(nil, []byte(value), &publicKeyArray, rand.Reader)
	if err != nil {
		return fmt.Errorf("encrypt BuildBuddy secret: %w", err)
	}
	return request("UpdateSecret", map[string]any{
		"secret": map[string]string{
			"name":  secretName,
			"value": base64.StdEncoding.EncodeToString(ciphertext),
		},
	}, nil)
}

func deleteSecret() error {
	return request("DeleteSecret", map[string]any{
		"secret": map[string]string{"name": secretName},
	}, nil)
}

func request(method string, payload any, response any) error {
	adminKey := os.Getenv(adminKeyEnv)
	if adminKey == "" {
		return fmt.Errorf("%s is required", adminKeyEnv)
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("encode BuildBuddy %s request: %w", method, err)
	}
	req, err := http.NewRequest(http.MethodPost, apiURL+method, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create BuildBuddy %s request: %w", method, err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-buildbuddy-api-key", adminKey)
	client := &http.Client{Timeout: 20 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("call BuildBuddy %s: %w", method, err)
	}
	defer resp.Body.Close()
	if method == "DeleteSecret" && resp.StatusCode == http.StatusNotFound {
		return nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("BuildBuddy %s returned HTTP %d", method, resp.StatusCode)
	}
	if response == nil {
		_, _ = io.Copy(io.Discard, resp.Body)
		return nil
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(response); err != nil {
		return fmt.Errorf("decode BuildBuddy %s response: %w", method, err)
	}
	return nil
}

func fatal(message string) {
	_, _ = fmt.Fprintln(os.Stderr, message)
	os.Exit(1)
}
