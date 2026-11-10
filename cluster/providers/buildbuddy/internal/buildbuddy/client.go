// Package buildbuddy implements the BuildBuddy organization secrets API.
package buildbuddy

import (
	"context"
	"crypto/rand"
	"crypto/tls"
	"encoding/base64"
	"errors"
	"fmt"
	"sort"
	"time"

	apikeypb "github.com/buildbuddy-io/buildbuddy/proto/api_key"
	servicepb "github.com/buildbuddy-io/buildbuddy/proto/buildbuddy_service"
	capabilitypb "github.com/buildbuddy-io/buildbuddy/proto/capability"
	contextpb "github.com/buildbuddy-io/buildbuddy/proto/context"
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

// APIKey contains only metadata returned by BuildBuddy's group-key list API.
// The generated key value is intentionally absent.
type APIKey struct {
	ID                  string
	Label               string
	Capabilities        []string
	VisibleToDevelopers bool
}

// CreatedAPIKey contains the one-time key value returned only when a key is
// created. Callers should publish Value directly as connection detail and must
// not log it or copy it into managed-resource status.
type CreatedAPIKey struct {
	APIKey
	Value []byte
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
type buildBuddyRPCError struct {
	method    string
	code      codes.Code
	ambiguous bool
}

func (e *buildBuddyRPCError) Error() string {
	return fmt.Sprintf("BuildBuddy %s failed: %s", e.method, e.code)
}

func (e *buildBuddyRPCError) AmbiguousAPIKeyCreate() bool {
	return e.method == "CreateApiKey" && e.ambiguous
}

func rpcError(method string, err error) error {
	code := status.Code(err)
	return &buildBuddyRPCError{
		method: method,
		code:   code,
		// These errors are returned by BuildBuddy before its CreateApiKey
		// handler can commit a key. Other RPC errors may occur after a commit
		// whose response did not reach this client.
		ambiguous: method == "CreateApiKey" && !definitivelyRejectedCreate(code),
	}
}

func definitivelyRejectedCreate(code codes.Code) bool {
	switch code {
	case codes.InvalidArgument, codes.Unauthenticated, codes.PermissionDenied, codes.NotFound, codes.FailedPrecondition, codes.Unimplemented:
		return true
	default:
		return false
	}
}

// IsAmbiguousAPIKeyCreate reports whether a CreateApiKey RPC may have
// committed even though no successful response reached the provider.
func IsAmbiguousAPIKeyCreate(err error) bool {
	var ambiguous interface{ AmbiguousAPIKeyCreate() bool }
	return errors.As(err, &ambiguous) && ambiguous.AmbiguousAPIKeyCreate()
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

// APIKeys lists group-owned API-key metadata. BuildBuddy deliberately omits
// values from this endpoint; callers must not use GetApiKey for routine
// observation because server-side value readback is optional.
func (c *Client) APIKeys(ctx context.Context, groupID string) ([]APIKey, error) {
	if groupID == "" {
		return nil, errors.New("BuildBuddy group ID is empty")
	}
	ctx, cancel := c.requestContext(ctx)
	defer cancel()
	response, err := c.rpc.GetApiKeys(ctx, &apikeypb.GetApiKeysRequest{
		RequestContext: &contextpb.RequestContext{GroupId: groupID},
		GroupId:        groupID,
	})
	if err != nil {
		return nil, rpcError("GetApiKeys", err)
	}
	keys := make([]APIKey, 0, len(response.GetApiKey()))
	for _, key := range response.GetApiKey() {
		metadata, err := apiKeyMetadata(key)
		if err != nil {
			return nil, err
		}
		keys = append(keys, metadata)
	}
	return keys, nil
}

// CreateAPIKey creates a group-owned API key. The one-time value is returned
// separately from metadata so callers can publish it as connection details.
func (c *Client) CreateAPIKey(ctx context.Context, groupID, label string, capabilities []string, visibleToDevelopers bool) (CreatedAPIKey, error) {
	protoCaps, err := protoCapabilities(capabilities)
	if err != nil {
		return CreatedAPIKey{}, err
	}
	if groupID == "" || label == "" {
		return CreatedAPIKey{}, errors.New("BuildBuddy API-key group ID and label are required")
	}
	ctx, cancel := c.requestContext(ctx)
	defer cancel()
	response, err := c.rpc.CreateApiKey(ctx, &apikeypb.CreateApiKeyRequest{
		RequestContext:      &contextpb.RequestContext{GroupId: groupID},
		Label:               label,
		Capability:          protoCaps,
		VisibleToDevelopers: visibleToDevelopers,
	})
	if err != nil {
		return CreatedAPIKey{}, rpcError("CreateApiKey", err)
	}
	key := response.GetApiKey()
	if key == nil || key.GetId() == "" || key.GetValue() == "" {
		return CreatedAPIKey{}, &buildBuddyRPCError{method: "CreateApiKey", code: codes.Unknown, ambiguous: true}
	}
	metadata, err := apiKeyMetadata(key)
	if err != nil {
		return CreatedAPIKey{}, &buildBuddyRPCError{method: "CreateApiKey", code: codes.Unknown, ambiguous: true}
	}
	return CreatedAPIKey{APIKey: metadata, Value: []byte(key.GetValue())}, nil
}

// UpdateAPIKey updates the mutable metadata of a group-owned key. Group
// membership is checked by the controller before calling this method.
func (c *Client) UpdateAPIKey(ctx context.Context, id, label string, capabilities []string, visibleToDevelopers bool) error {
	protoCaps, err := protoCapabilities(capabilities)
	if err != nil {
		return err
	}
	if id == "" || label == "" {
		return errors.New("BuildBuddy API-key ID and label are required")
	}
	ctx, cancel := c.requestContext(ctx)
	defer cancel()
	_, err = c.rpc.UpdateApiKey(ctx, &apikeypb.UpdateApiKeyRequest{
		Id:                  id,
		Label:               label,
		Capability:          protoCaps,
		VisibleToDevelopers: visibleToDevelopers,
	})
	if err != nil {
		return rpcError("UpdateApiKey", err)
	}
	return nil
}

// DeleteAPIKey revokes a BuildBuddy API key. NotFound is idempotent success.
func (c *Client) DeleteAPIKey(ctx context.Context, id string) error {
	if id == "" {
		return errors.New("BuildBuddy API-key ID is empty")
	}
	ctx, cancel := c.requestContext(ctx)
	defer cancel()
	_, err := c.rpc.DeleteApiKey(ctx, &apikeypb.DeleteApiKeyRequest{Id: id})
	if err != nil && status.Code(err) != codes.NotFound {
		return rpcError("DeleteApiKey", err)
	}
	return nil
}

func apiKeyMetadata(key *apikeypb.ApiKey) (APIKey, error) {
	if key == nil || key.GetId() == "" {
		return APIKey{}, errors.New("BuildBuddy returned API-key metadata without an ID")
	}
	capabilities := make([]string, 0, len(key.GetCapability()))
	for _, capability := range key.GetCapability() {
		name, ok := capabilitypb.Capability_name[int32(capability)]
		if !ok || name == "UNKNOWN_CAPABILITY" {
			return APIKey{}, errors.New("BuildBuddy returned an unknown API-key capability")
		}
		capabilities = append(capabilities, name)
	}
	sort.Strings(capabilities)
	return APIKey{
		ID:                  key.GetId(),
		Label:               key.GetLabel(),
		Capabilities:        capabilities,
		VisibleToDevelopers: key.GetVisibleToDevelopers(),
	}, nil
}

func protoCapabilities(capabilities []string) ([]capabilitypb.Capability, error) {
	protoCaps := make([]capabilitypb.Capability, 0, len(capabilities))
	for _, capability := range capabilities {
		value, ok := capabilitypb.Capability_value[capability]
		if !ok || value == int32(capabilitypb.Capability_UNKNOWN_CAPABILITY) {
			return nil, fmt.Errorf("unsupported BuildBuddy API-key capability %q", capability)
		}
		protoCaps = append(protoCaps, capabilitypb.Capability(value))
	}
	return protoCaps, nil
}
