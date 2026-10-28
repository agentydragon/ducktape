package push

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/ecdh"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/hkdf"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

// browser is a test browser's push subscription: the keys it registers, which decrypt what is pushed to it.
type browser struct {
	endpoint string
	private  *ecdh.PrivateKey
	auth     []byte
}

// newBrowser subscribes at https://push.test/<name>.
func newBrowser(t *testing.T, name string) browser {
	t.Helper()
	private, err := ecdh.P256().GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	auth := make([]byte, 16)
	if _, err := rand.Read(auth); err != nil {
		t.Fatal(err)
	}
	return browser{endpoint: "https://push.test/" + name, private: private, auth: auth}
}

func (b browser) registration() Registration {
	return Registration{
		Endpoint: b.endpoint,
		P256dh:   base64.RawURLEncoding.EncodeToString(b.private.PublicKey().Bytes()),
		Auth:     base64.RawURLEncoding.EncodeToString(b.auth),
	}
}

// decrypt reads an `aes128gcm` body (RFC 8188) as RFC 8291 has the browser derive its key.
func (b browser) decrypt(t *testing.T, body []byte) []byte {
	t.Helper()
	if len(body) < 21 || len(body) < 21+int(body[20]) {
		t.Fatalf("a body of %d bytes has no aes128gcm header", len(body))
	}
	salt, recordSize, keyID := body[:16], binary.BigEndian.Uint32(body[16:20]), body[21:21+int(body[20])]
	ciphertext := body[21+len(keyID):]
	if len(ciphertext) > int(recordSize) {
		t.Fatalf("a record of %d bytes, beyond the record size %d", len(ciphertext), recordSize)
	}
	server, err := ecdh.P256().NewPublicKey(keyID)
	if err != nil {
		t.Fatal(err)
	}
	secret, err := b.private.ECDH(server)
	if err != nil {
		t.Fatal(err)
	}
	ikm, err := hkdf.Key(sha256.New, secret, b.auth, "WebPush: info\x00"+string(b.private.PublicKey().Bytes())+string(keyID), 32)
	if err != nil {
		t.Fatal(err)
	}
	key, err := hkdf.Key(sha256.New, ikm, salt, "Content-Encoding: aes128gcm\x00", 16)
	if err != nil {
		t.Fatal(err)
	}
	nonce, err := hkdf.Key(sha256.New, ikm, salt, "Content-Encoding: nonce\x00", 12)
	if err != nil {
		t.Fatal(err)
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		t.Fatal(err)
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		t.Fatal(err)
	}
	plaintext, err := aead.Open(nil, nonce, ciphertext, nil)
	if err != nil {
		t.Fatalf("the push does not decrypt with the browser's keys: %v", err)
	}
	// The one record is the last: its content, the delimiter 2, then zeros of padding.
	plaintext = bytes.TrimRight(plaintext, "\x00")
	if !bytes.HasSuffix(plaintext, []byte{2}) {
		t.Fatal("the record does not end with the last record's delimiter")
	}
	return plaintext[:len(plaintext)-1]
}

// message is a push's payload, as the service worker reads it.
type message struct {
	Kind        string `json:"kind"`
	ActionID    string `json:"action_id"`
	ActionGroup string `json:"action_group"`
	ActionName  string `json:"action_name"`
	Version     int64  `json:"version"`
	URL         string `json:"url"`
}

// received is a push as the push service received it.
type received struct {
	header http.Header
	body   []byte
}

// pushService is the stand-in for the browsers' push services: each browser's endpoint path is its subscription
// there. It answers each push with answer's status, 201 by default.
type pushService struct {
	server *httptest.Server
	mu     sync.Mutex
	// pushes holds each endpoint's pushes, in arrival order.
	pushes map[string][]received
	answer func(endpoint string) int
	// arrived holds a signal once a push arrived since it was last received.
	arrived chan struct{}
}

func newPushService(t *testing.T) *pushService {
	t.Helper()
	s := &pushService{
		pushes: map[string][]received{}, answer: func(string) int { return http.StatusCreated }, arrived: make(chan struct{}, 1),
	}
	s.server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			return
		}
		endpoint := "https://push.test" + r.URL.Path
		s.mu.Lock()
		s.pushes[endpoint] = append(s.pushes[endpoint], received{header: r.Header.Clone(), body: body})
		answer := s.answer
		s.mu.Unlock()
		select {
		case s.arrived <- struct{}{}:
		default:
		}
		w.WriteHeader(answer(endpoint))
	}))
	t.Cleanup(s.server.Close)
	return s
}

// transport reaches the push service at every address, as push.test.
func (s *pushService) transport() http.RoundTripper {
	transport := s.server.Client().Transport.(*http.Transport).Clone()
	// The name httptest's certificate is issued for.
	transport.TLSClientConfig.ServerName = "example.com"
	address := s.server.Listener.Addr().String()
	transport.DialContext = func(ctx context.Context, network, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, address)
	}
	return transport
}

func (s *pushService) answering(answer func(endpoint string) int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.answer = answer
}

// take returns the pushes the browser received since the last take.
func (s *pushService) take(b browser) []received {
	s.mu.Lock()
	defer s.mu.Unlock()
	pushes := s.pushes[b.endpoint]
	delete(s.pushes, b.endpoint)
	return pushes
}

// await waits until the browser received count pushes since the last take, and takes them.
func (s *pushService) await(t *testing.T, b browser, count int) []received {
	t.Helper()
	timeout := time.After(awaitBound)
	for {
		s.mu.Lock()
		arrived := len(s.pushes[b.endpoint])
		s.mu.Unlock()
		if arrived >= count {
			return s.take(b)
		}
		select {
		case <-s.arrived:
		case <-timeout:
			t.Fatalf("%s received %d pushes, want %d", b.endpoint, arrived, count)
		}
	}
}

// read checks a push as the browser's push service and the browser check it, and returns its payload: the VAPID
// header that names applicationServerKey and is signed by its private key, for the push service's origin; the
// delivery headers; the payload encrypted to the browser.
func (b browser) read(t *testing.T, push received, applicationServerKey string) message {
	t.Helper()
	for header, want := range map[string]string{
		"Content-Encoding": "aes128gcm", "Ttl": "600", "Urgency": "high", "Topic": "agentplane-actions",
	} {
		if got := push.header.Get(header); got != want {
			t.Errorf("%s: %q, want %q", header, got, want)
		}
	}
	token, key, found := strings.Cut(strings.TrimPrefix(push.header.Get("Authorization"), "vapid t="), ", k=")
	if !found || key != applicationServerKey {
		t.Fatalf("Authorization %q names another key than %q", push.header.Get("Authorization"), applicationServerKey)
	}
	point, err := base64.RawURLEncoding.DecodeString(key)
	if err != nil {
		t.Fatal(err)
	}
	public, err := ecdsa.ParseUncompressedPublicKey(elliptic.P256(), point)
	if err != nil {
		t.Fatal(err)
	}
	signed, err := jwt.ParseSigned(token, []jose.SignatureAlgorithm{jose.ES256})
	if err != nil {
		t.Fatal(err)
	}
	var claims jwt.Claims
	if err := signed.Claims(public, &claims); err != nil {
		t.Fatalf("the VAPID token is not signed by the application server key: %v", err)
	}
	// RFC 8292: the audience is the push service's origin, and the token expires within a day.
	if err := claims.ValidateWithLeeway(jwt.Expected{
		AnyAudience: jwt.Audience{"https://push.test"}, Subject: testSubject, Time: time.Now(),
	}, 0); err != nil || claims.Expiry == nil || claims.Expiry.Time().After(time.Now().Add(24*time.Hour)) {
		t.Errorf("VAPID claims %+v: %v", claims, err)
	}
	var got message
	if err := json.Unmarshal(b.decrypt(t, push.body), &got); err != nil {
		t.Fatal(err)
	}
	return got
}
