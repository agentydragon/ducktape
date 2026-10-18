// Package presentation finds where a credential's placeholder sits in a request and puts the
// real value there.
//
// There is one decomposition per target, and both halves of the decision read it: Present finds a
// credential's placeholder and hands back the rebuilders that same parse produced, so a placeholder
// the proxy recognised cannot be one it forwards. A detector and a substituter that each decompose
// the value their own way is the failure this package exists to foreclose.
//
// A component must equal the placeholder exactly. A placeholder that is merely a substring of one
// is not presented, so it is neither detected nor substituted and reaches the upstream inert --
// which is the point: it is not a secret, and a request that splices a real credential into the
// middle of an unrelated string is the thing worth making impossible.
package presentation

import (
	"encoding/base64"
	"net/http"
	"net/textproto"
	"slices"
	"strings"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/egress/resources"
)

const basic = "basic"

// Parsed is one header value decomposed by one target: the credential component it carries, and
// how to rebuild the value around a different one.
type Parsed struct {
	Component string
	Rebuild   func(credential string) string
}

// Parse returns the credential component target reads out of this header value, or false when the
// value is not of that target's shape.
func Parse(target resources.Target, value string) (Parsed, bool) {
	switch target := target.(type) {
	case resources.WholeValue:
		return Parsed{Component: value, Rebuild: func(credential string) string { return credential }}, true
	case resources.SchemeToken:
		sent, token, found := strings.Cut(value, " ")
		if !found || !strings.EqualFold(sent, target.Scheme) {
			return Parsed{}, false
		}
		// The scheme goes back as the client sent it: clients disagree on case, and git's own
		// documented form is a lowercase `bearer`.
		return Parsed{Component: token, Rebuild: func(credential string) string { return sent + " " + credential }}, true
	case resources.BasicWhole:
		scheme, payload, ok := basicPayload(value)
		if !ok {
			return Parsed{}, false
		}
		return Parsed{Component: payload, Rebuild: func(credential string) string { return basicValue(scheme, credential) }}, true
	case resources.BasicUsername:
		scheme, username, password, ok := basicPair(value)
		if !ok {
			return Parsed{}, false
		}
		return Parsed{Component: username, Rebuild: func(credential string) string {
			return basicValue(scheme, credential+":"+password)
		}}, true
	case resources.BasicPassword:
		scheme, username, password, ok := basicPair(value)
		if !ok {
			return Parsed{}, false
		}
		return Parsed{Component: password, Rebuild: func(credential string) string {
			return basicValue(scheme, username+":"+credential)
		}}, true
	}
	return Parsed{}, false
}

// basicPayload returns the scheme as sent and the decoded payload, or false when the value is
// not a `Basic` one.
func basicPayload(value string) (scheme, payload string, ok bool) {
	scheme, encoded, found := strings.Cut(value, " ")
	if !found || !strings.EqualFold(scheme, basic) {
		return "", "", false
	}
	decoded, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil || !utf8.Valid(decoded) {
		return "", "", false
	}
	return scheme, string(decoded), true
}

// basicPair returns scheme, username and password, or false when the value is not `Basic` or its
// payload carries no `:` to split on -- a payload without one is basicWhole's case, not these two's.
func basicPair(value string) (scheme, username, password string, ok bool) {
	scheme, payload, ok := basicPayload(value)
	if !ok {
		return "", "", "", false
	}
	username, password, found := strings.Cut(payload, ":")
	return scheme, username, password, found
}

func basicValue(scheme, payload string) string {
	return scheme + " " + base64.StdEncoding.EncodeToString([]byte(payload))
}

// HeaderRewrite is one header as it must be forwarded: all of its values, in order, with the real
// credential in every position that presented the placeholder and the rest untouched.
type HeaderRewrite struct {
	Header string
	Values []string
}

// Apply sets the header on h under the spelling the credential declared, replacing every value
// the client sent whatever case it used.
func (r HeaderRewrite) Apply(h http.Header) {
	delete(h, textproto.CanonicalMIMEHeaderKey(r.Header))
	h[r.Header] = r.Values
}

func (HeaderRewrite) String() string   { return "HeaderRewrite{redacted}" }
func (HeaderRewrite) GoString() string { return "HeaderRewrite{redacted}" }

type headerPresentation struct {
	header     string
	rebuilders []func(string) string
}

// Presentation is one credential as a request actually presents it: for every header carrying
// its placeholder, how to rebuild that header's values around the real value, and the JSON body
// fields that carry it.
type Presentation struct {
	headers []headerPresentation
	body    *bodyPresentation
}

// Rewrites returns the headers to forward with credential in place of the placeholder.
func (p Presentation) Rewrites(credential string) []HeaderRewrite {
	rewrites := make([]HeaderRewrite, 0, len(p.headers))
	for _, header := range p.headers {
		values := make([]string, len(header.rebuilders))
		for i, rebuild := range header.rebuilders {
			values[i] = rebuild(credential)
		}
		rewrites = append(rewrites, HeaderRewrite{Header: header.header, Values: values})
	}
	return rewrites
}

// BodyRewrite returns the JSON body to forward with credential in place of the placeholder in
// every field that presented it, or nil when the request did not present it in its body. It fails
// when the credential cannot be written as a JSON string.
func (p Presentation) BodyRewrite(credential string) (*BodyRewrite, error) {
	if p.body == nil {
		return nil, nil
	}
	return p.body.rewrite(credential)
}

// Present returns where credential's placeholder sits in these headers and JSON body (nil when
// the request has none), or false when it is not presented.
func Present(credential resources.Credential, headers http.Header, body *JSONObject) (Presentation, bool) {
	placeholder := credential.Placeholder()
	var presented []headerPresentation
	for _, name := range headersOf(credential) {
		values := headers.Values(name.canonical)
		rebuilders := make([]func(string) string, len(values))
		matched := false
		for i, value := range values {
			if rebuild, ok := rebuildOf(credential, name.canonical, value, placeholder); ok {
				matched = true
				rebuilders[i] = rebuild
			} else {
				rebuilders[i] = func(string) string { return value }
			}
		}
		if matched {
			presented = append(presented, headerPresentation{header: name.declared, rebuilders: rebuilders})
		}
	}
	var fields []string
	for _, target := range credential.Targets {
		if target, ok := target.(resources.JSONField); ok && body.hasString(target.Field, placeholder) {
			fields = append(fields, target.Field)
		}
	}
	var presentedBody *bodyPresentation
	if len(fields) > 0 {
		presentedBody = &bodyPresentation{document: body, fields: fields}
	}
	if len(presented) == 0 && presentedBody == nil {
		return Presentation{}, false
	}
	return Presentation{headers: presented, body: presentedBody}, true
}

type declaredHeader struct{ canonical, declared string }

// headersOf returns the headers the credential's targets name, in declaration order, each once:
// the canonical key to look the client's header up by, and the spelling the policy declared --
// which is the spelling the rewritten header goes out under, whatever case the client sent.
func headersOf(credential resources.Credential) []declaredHeader {
	var headers []declaredHeader
	for _, target := range credential.Targets {
		header, ok := headerOf(target)
		if !ok {
			continue
		}
		canonical := textproto.CanonicalMIMEHeaderKey(header)
		if !slices.ContainsFunc(headers, func(seen declaredHeader) bool { return seen.canonical == canonical }) {
			headers = append(headers, declaredHeader{canonical: canonical, declared: header})
		}
	}
	return headers
}

func headerOf(target resources.Target) (string, bool) {
	switch target := target.(type) {
	case resources.WholeValue:
		return target.Header, true
	case resources.SchemeToken:
		return target.Header, true
	case resources.BasicUsername:
		return target.Header, true
	case resources.BasicPassword:
		return target.Header, true
	case resources.BasicWhole:
		return target.Header, true
	}
	return "", false
}

// rebuildOf returns the rebuild of the first target on the canonical header whose component is
// exactly the placeholder. Which target that is never turns on order in practice: a value is of at
// most one target's shape, since a `Basic` payload is not a `Bearer` token and neither is the other.
func rebuildOf(credential resources.Credential, canonical, value, placeholder string) (func(string) string, bool) {
	for _, target := range credential.Targets {
		header, ok := headerOf(target)
		if !ok || textproto.CanonicalMIMEHeaderKey(header) != canonical {
			continue
		}
		if parsed, ok := Parse(target, value); ok && parsed.Component == placeholder {
			return parsed.Rebuild, true
		}
	}
	return nil, false
}
