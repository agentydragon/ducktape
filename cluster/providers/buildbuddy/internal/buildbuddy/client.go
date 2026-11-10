// Package buildbuddy implements the BuildBuddy organization secrets API.
package buildbuddy

import (
	"context"
	"crypto/rand"
	"crypto/tls"
	"encoding/base64"
	"errors"
	"fmt"
	"time"

	servicepb "github.com/buildbuddy-io/buildbuddy/proto/buildbuddy_service"
	secretspb "github.com/buildbuddy-io/buildbuddy/proto/secrets"
	"golang.org/x/crypto/nacl/box"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const requestTimeout = 20 * time.Second

type Client struct {
	rpc    servicepb.BuildBuddyServiceClient
	conn   *grpc.ClientConn
	apiKey string
}

type PublicKey struct {
	ID    string
	Value string
}

// NewClient opens a TLS connection to BuildBuddy's gRPC service. Dialing is lazy.
func NewClient(apiKey string) (*Client, error) {
	if apiKey == "" {
		return nil, errors.New("BuildBuddy API key is empty")
	}
	conn, err := grpc.NewClient("dns:///app.buildbuddy.io:443", grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS12})))
	if err != nil {
		return nil, fmt.Errorf("create BuildBuddy connection: %w", err)
	}
	return &Client{rpc: servicepb.NewBuildBuddyServiceClient(conn), conn: conn, apiKey: apiKey}, nil
}

func (c *Client) Close() error { return c.conn.Close() }

func (c *Client) requestContext(ctx context.Context) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithTimeout(ctx, requestTimeout)
	return metadata.AppendToOutgoingContext(ctx, "x-buildbuddy-api-key", c.apiKey), cancel
}

// Do not include server error messages: they may contain sensitive request data.
func rpcError(method string, err error) error {
	return fmt.Errorf("BuildBuddy %s failed: %s", method, status.Code(err))
}

func (c *Client) PublicKey(ctx context.Context) (PublicKey, error) {
	ctx, cancel := c.requestContext(ctx)
	defer cancel()
	response, err := c.rpc.GetPublicKey(ctx, &secretspb.GetPublicKeyRequest{})
	if err != nil {
		return PublicKey{}, rpcError("GetPublicKey", err)
	}
	key := response.GetPublicKey()
	if key.GetId() == "" || key.GetValue() == "" {
		return PublicKey{}, errors.New("BuildBuddy returned an incomplete public key")
	}
	return PublicKey{ID: key.GetId(), Value: key.GetValue()}, nil
}

func (c *Client) Exists(ctx context.Context, name string) (bool, error) {
	ctx, cancel := c.requestContext(ctx)
	defer cancel()
	response, err := c.rpc.ListSecrets(ctx, &secretspb.ListSecretsRequest{})
	if err != nil {
		return false, rpcError("ListSecrets", err)
	}
	for _, secret := range response.GetSecret() {
		if secret.GetName() == name {
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
	ctx, cancel := c.requestContext(ctx)
	defer cancel()
	_, err = c.rpc.UpdateSecret(ctx, &secretspb.UpdateSecretRequest{Secret: &secretspb.Secret{Name: name, Value: base64.StdEncoding.EncodeToString(sealed)}})
	if err != nil {
		return rpcError("UpdateSecret", err)
	}
	return nil
}

func (c *Client) Delete(ctx context.Context, name string) error {
	ctx, cancel := c.requestContext(ctx)
	defer cancel()
	_, err := c.rpc.DeleteSecret(ctx, &secretspb.DeleteSecretRequest{Secret: &secretspb.Secret{Name: name}})
	if err != nil && status.Code(err) != codes.NotFound {
		return rpcError("DeleteSecret", err)
	}
	return nil
}
