package auth

import "github.com/gorilla/securecookie"

const sessionCookiePurpose = "agentplane-session"

// cookieSigner signs the opaque database-session handle with Gorilla's maintained
// securecookie implementation. The cookie is intentionally not wire-compatible
// with the former Python itsdangerous cookie.
type cookieSigner struct{ cookie *securecookie.SecureCookie }

func newCookieSigner(secret string, maxAgeSeconds int) *cookieSigner {
	cookie := securecookie.New([]byte(secret), nil)
	cookie.MaxAge(maxAgeSeconds)
	return &cookieSigner{cookie: cookie}
}

func (s *cookieSigner) sign(handle string) (string, error) {
	return s.cookie.Encode(sessionCookiePurpose, handle)
}

func (s *cookieSigner) unsign(value string) (string, error) {
	var handle string
	if err := s.cookie.Decode(sessionCookiePurpose, value, &handle); err != nil {
		return "", err
	}
	return handle, nil
}
