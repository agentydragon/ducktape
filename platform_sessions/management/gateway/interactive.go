package gateway

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"time"

	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/providerpb"
	"github.com/coder/websocket"
	"google.golang.org/grpc"
)

const maxInteractiveFrameBytes = 32 << 20

// interactiveOrigin requires the public origin on every upgrade. Browser
// WebSockets carry cookies and therefore cannot use the GET exemption for
// ordinary management reads.
func interactiveOrigin(r *http.Request, publicOrigin string) bool {
	if len(r.Header.Values("Origin")) != 1 || r.Header.Get("Origin") != publicOrigin {
		return false
	}
	site := r.Header.Get("Sec-Fetch-Site")
	return site == "" || site == "same-origin" || site == "none"
}

func interactiveCapability(ctx context.Context, p Provider) (bool, error) {
	info, err := p.Client.GetInfo(ctx, &providerpb.GetInfoRequest{}, grpc.WaitForReady(false))
	if err != nil || info == nil || info.GetProvider().GetProviderId() != p.Ref.GetProviderId() || !providerInfoValid(info.GetProvider()) {
		return false, errProviderUnavailable
	}
	for _, instance := range info.GetProvider().GetInstances() {
		if instance.GetInstanceId() == p.Ref.GetInstanceId() {
			return instance.GetInteractiveSignIn() != nil, nil
		}
	}
	return false, errProviderUnavailable
}

// interactiveWebSocket only accepts an owner browser cookie and bridges binary
// RFB frames to the configured provider's fixed internal endpoint. Neither the
// caller nor provider metadata supplies a network target or credentials.
func interactiveWebSocket(owner *ownerAuth, service *Service, providers map[string]ProviderHTTPConfig) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !interactiveOrigin(r, owner.cfg.PublicBaseURL) {
			http.Error(w, "invalid origin", http.StatusForbidden)
			return
		}
		if len(r.Header.Values("Authorization")) != 0 || !owner.loggedIn(r) {
			http.Error(w, "owner sign-in required", http.StatusUnauthorized)
			return
		}
		ownerCookieValue, _ := r.Cookie(ownerCookie) // loggedIn validated this cookie above
		var session ownerSession
		if owner.cookie.Decode(ownerCookie, ownerCookieValue.Value, &session) != nil {
			http.Error(w, "owner sign-in required", http.StatusUnauthorized)
			return
		}
		streamCtx, stopStream := context.WithDeadline(r.Context(), time.Unix(session.ExpiresAt, 0))
		defer stopStream()
		instanceID := r.PathValue("instance")
		if !validID.MatchString(instanceID) || r.URL.RawQuery != "" {
			http.NotFound(w, r)
			return
		}
		provider, ok := service.providers[instanceID]
		if !ok {
			http.NotFound(w, r)
			return
		}
		available, err := interactiveCapability(streamCtx, provider)
		if err != nil {
			http.Error(w, "provider unavailable", http.StatusBadGateway)
			return
		}
		if !available {
			http.NotFound(w, r)
			return
		}
		config := providers[instanceID]
		client, err := providerHTTPClient(config.CAFile)
		if err != nil {
			http.Error(w, "provider unavailable", http.StatusBadGateway)
			return
		}
		defer client.CloseIdleConnections()
		origin, _ := url.Parse(config.Origin) // validated by HTTPServer
		target := "wss://" + origin.Host + "/internal/interactive-sign-in/" + instanceID
		dialCtx, cancelDial := context.WithTimeout(streamCtx, 20*time.Second)
		upstream, _, err := websocket.Dial(dialCtx, target, &websocket.DialOptions{
			HTTPClient:   client,
			HTTPHeader:   http.Header{"Authorization": []string{"Bearer " + config.OwnerToken}},
			Subprotocols: []string{"binary"},
		})
		cancelDial()
		if err != nil {
			http.Error(w, "provider unavailable", http.StatusBadGateway)
			return
		}
		defer upstream.CloseNow()
		browser, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true, Subprotocols: []string{"binary"}}) // exact Origin checked above
		if err != nil {
			return
		}
		defer browser.CloseNow()
		upstream.SetReadLimit(maxInteractiveFrameBytes)
		browser.SetReadLimit(maxInteractiveFrameBytes)
		ctx, cancel := context.WithCancel(streamCtx)
		defer cancel()
		done := make(chan struct{}, 2)
		go func() { relayBinary(ctx, upstream, browser); done <- struct{}{} }()
		go func() { relayBinary(ctx, browser, upstream); done <- struct{}{} }()
		<-done
		cancel()
		_ = browser.CloseNow()
		_ = upstream.CloseNow()
		<-done
	}
}

func relayBinary(ctx context.Context, source, destination *websocket.Conn) {
	for {
		kind, payload, err := source.Read(ctx)
		if err != nil || kind != websocket.MessageBinary {
			return
		}
		if destination.Write(ctx, websocket.MessageBinary, payload) != nil {
			return
		}
	}
}

var errProviderUnavailable = errors.New("provider unavailable")
