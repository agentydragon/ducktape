// Package buildbuddy implements the BuildBuddy organization secrets API.
package buildbuddy

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"golang.org/x/crypto/nacl/box"
)

const DefaultURL = "https://app.buildbuddy.io/rpc/BuildBuddyService/"

type Client struct {
	URL    string
	APIKey string
	HTTP   *http.Client
}

type PublicKey struct {
	ID    string `json:"id"`
	Value string `json:"value"`
}

func (c *Client) request(ctx context.Context, method string, payload any, result any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.URL+method, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-buildbuddy-api-key", c.APIKey)
	httpClient := c.HTTP
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 20 * time.Second}
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("BuildBuddy %s: %w", method, err)
	}
	defer resp.Body.Close()
	if method == "DeleteSecret" && resp.StatusCode == http.StatusNotFound {
		return nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("BuildBuddy %s returned HTTP %d", method, resp.StatusCode)
	}
	if result == nil {
		_, err = io.Copy(io.Discard, resp.Body)
		return err
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(result)
}

func (c *Client) PublicKey(ctx context.Context) (PublicKey, error) {
	var response struct {
		PublicKey PublicKey `json:"publicKey"`
	}
	if err := c.request(ctx, "GetPublicKey", struct{}{}, &response); err != nil {
		return PublicKey{}, err
	}
	if response.PublicKey.ID == "" || response.PublicKey.Value == "" {
		return PublicKey{}, errors.New("BuildBuddy returned an incomplete public key")
	}
	return response.PublicKey, nil
}

func (c *Client) Exists(ctx context.Context, name string) (bool, error) {
	var response struct {
		Secrets []struct {
			Name string `json:"name"`
		} `json:"secret"`
	}
	if err := c.request(ctx, "ListSecrets", struct{}{}, &response); err != nil {
		return false, err
	}
	for _, secret := range response.Secrets {
		if secret.Name == name {
			return true, nil
		}
	}
	return false, nil
}

func (c *Client) Put(ctx context.Context, name string, value []byte, key PublicKey) error {
	decoded, err := base64.StdEncoding.DecodeString(key.Value)
	var publicKey [32]byte
	if err != nil || len(decoded) != len(publicKey) {
		return errors.New("BuildBuddy returned an invalid public key")
	}
	if len(value) == 0 {
		return errors.New("source secret value is empty")
	}
	copy(publicKey[:], decoded)
	sealed, err := box.SealAnonymous(nil, value, &publicKey, rand.Reader)
	if err != nil {
		return fmt.Errorf("seal BuildBuddy secret: %w", err)
	}
	return c.request(ctx, "UpdateSecret", map[string]any{"secret": map[string]string{
		"name": name, "value": base64.StdEncoding.EncodeToString(sealed),
	}}, nil)
}

func (c *Client) Delete(ctx context.Context, name string) error {
	return c.request(ctx, "DeleteSecret", map[string]any{"secret": map[string]string{"name": name}}, nil)
}
