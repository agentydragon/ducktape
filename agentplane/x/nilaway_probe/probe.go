// Package nilawayprobe isolates which nil flows NilAway models (spike for issue #15098).
package nilawayprobe

import (
	"io"
	"net/http"
	"net/http/httputil"
	"os"
	"testing"
)

type capture struct {
	io.ReadCloser
}

type local struct {
	Body io.ReadCloser
}

// Case A: read an external struct's interface field and call a method on it.
func A(r *http.Request) { _, _ = r.Body.Read(nil) }

// Case B: wrap an external struct's interface field in an embedding struct, then call through it.
func B(r *http.Request) { c := &capture{ReadCloser: r.Body}; _, _ = c.Read(nil) }

// Case C: embed a literal nil, then call through the promoted method.
func C() { c := &capture{ReadCloser: nil}; _, _ = c.Read(nil) }

// Case D: same as C but with an explicit field instead of embedding.
func D() { l := &local{Body: nil}; _, _ = l.Body.Read(nil) }

// Case E: literal nil interface method call.
func E() { var rc io.ReadCloser; _, _ = rc.Read(nil) }

// Case F: the bug's shape, inside a Rewrite closure.
func F() *httputil.ReverseProxy {
	return &httputil.ReverseProxy{Rewrite: func(out *httputil.ProxyRequest) {
		c := &capture{ReadCloser: out.Out.Body}
		out.Out.Body = c
		_, _ = c.Read(nil)
	}}
}

// Case G: explicit nil check makes Body known-nilable, then embedded.
func G(r *http.Request) {
	if r.Body == nil {
		c := &capture{ReadCloser: r.Body}
		_, _ = c.Read(nil)
	}
}

// Case H: explicit nil check, then direct call.
func H(r *http.Request) {
	if r.Body == nil {
		_, _ = r.Body.Read(nil)
	}
}

// Case I: nil check on an in-scope struct's interface field, then call.
func I(l *local) {
	if l.Body == nil {
		_, _ = l.Body.Read(nil)
	}
}

// Case J: nil check on an external struct's pointer field, then dereference.
func J(r *http.Request) {
	if r.URL == nil {
		_ = r.URL.Path
	}
}

// Case K: nil check on a local copy of the external field, then call.
func K(r *http.Request) {
	b := r.Body
	if b == nil {
		_, _ = b.Read(nil)
	}
}

// Case L: explicit embedded-field selector on a nil embed.
func L() { c := &capture{ReadCloser: nil}; _, _ = c.ReadCloser.Read(nil) }

func mk() (*local, error) { return nil, nil }

// Case M: error guarded by t.Fatal.
func M(t *testing.T) {
	l, err := mk()
	if err != nil {
		t.Fatal(err)
	}
	_ = l.Body
}

// Case N: error guarded by os.Exit.
func N() {
	l, err := mk()
	if err != nil {
		os.Exit(1)
	}
	_ = l.Body
}

// Case O: error guarded by panic.
func O() {
	l, err := mk()
	if err != nil {
		panic(err)
	}
	_ = l.Body
}

// Case P: error guarded by return.
func P() {
	l, err := mk()
	if err != nil {
		return
	}
	_ = l.Body
}
