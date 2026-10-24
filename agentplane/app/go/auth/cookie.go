package auth

import (
	"crypto/hmac"
	"crypto/sha1"
	"crypto/subtle"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
)

const signerSalt = "agentplane-operator-session-v1"

// cookieSigner is wire-compatible with itsdangerous.TimestampSigner using its
// default django-concat key derivation and SHA-1 digest. The cookie is
// <opaque-handle>.<base64url timestamp>.<base64url HMAC>.
type cookieSigner struct{ key []byte }

func newCookieSigner(secret string) *cookieSigner {
	derived := sha1.Sum([]byte(signerSalt + "signer" + secret))
	return &cookieSigner{key: derived[:]}
}

func (s *cookieSigner) sign(handle string, at time.Time) string {
	stamp := make([]byte, 4)
	binary.BigEndian.PutUint32(stamp, uint32(at.Unix()))
	message := handle + "." + base64.RawURLEncoding.EncodeToString(stamp)
	mac := hmac.New(sha1.New, s.key)
	_, _ = mac.Write([]byte(message))
	return message + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func (s *cookieSigner) unsign(value string, at time.Time, maxAge time.Duration) (string, error) {
	parts := strings.Split(value, ".")
	if len(parts) != 3 || parts[0] == "" {
		return "", errors.New("invalid signed session cookie")
	}
	stamp, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil || len(stamp) != 4 {
		return "", errors.New("invalid signed session cookie timestamp")
	}
	provided, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return "", errors.New("invalid signed session cookie signature")
	}
	message := parts[0] + "." + parts[1]
	mac := hmac.New(sha1.New, s.key)
	_, _ = mac.Write([]byte(message))
	want := mac.Sum(nil)
	if len(provided) != len(want) || subtle.ConstantTimeCompare(provided, want) != 1 {
		return "", errors.New("invalid signed session cookie signature")
	}
	seconds := int64(binary.BigEndian.Uint32(stamp))
	issued := time.Unix(seconds, 0)
	if issued.After(at) {
		return "", fmt.Errorf("signed session cookie timestamp is in the future")
	}
	if maxAge >= 0 && at.Sub(issued) > maxAge {
		return "", fmt.Errorf("signed session cookie is too old")
	}
	return parts[0], nil
}

func parseUnixSeconds(value any) (time.Time, bool) {
	switch typed := value.(type) {
	case float64:
		return time.Unix(int64(typed), int64((typed-float64(int64(typed)))*1e9)), true
	case json.Number:
		seconds, err := strconv.ParseFloat(typed.String(), 64)
		if err == nil {
			return time.Unix(int64(seconds), int64((seconds-float64(int64(seconds)))*1e9)), true
		}
	case int64:
		return time.Unix(typed, 0), true
	case int:
		return time.Unix(int64(typed), 0), true
	}
	return time.Time{}, false
}
