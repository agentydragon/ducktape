package presentation

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/egress/resources"
)

const (
	authorization = "Authorization"
	secretValue   = "the-real-value"
)

var (
	bearer        = resources.SchemeToken{Header: authorization, Scheme: "Bearer"}
	whole         = resources.WholeValue{Header: "X-Api-Key"}
	basicUsername = resources.BasicUsername{Header: authorization}
	basicPassword = resources.BasicPassword{Header: authorization}
	basicWhole    = resources.BasicWhole{Header: authorization}
	placeholder   = resources.Placeholder("github-pat")
)

func basicOf(payload string) string {
	return "Basic " + base64.StdEncoding.EncodeToString([]byte(payload))
}

func credential(name string, targets ...resources.Target) resources.Credential {
	return resources.Credential{
		Name: name, Description: "the " + name + " test credential",
		Source:  resources.SecretKey{Name: "pat", Key: "token"},
		Targets: targets,
	}
}

func TestParseReadsItsComponentAndPutsANewOneBack(t *testing.T) {
	for _, tc := range []struct {
		target           resources.Target
		value, component string
	}{
		{whole, "abc", "abc"},
		{bearer, "Bearer abc", "abc"},
		{bearer, "bearer abc", "abc"},
		{basicUsername, basicOf("abc:secret"), "abc"},
		{basicPassword, basicOf("user:abc"), "abc"},
		{basicPassword, basicOf("user:with:colons"), "with:colons"},
		{basicWhole, basicOf("abc"), "abc"},
		{basicWhole, basicOf("a:b"), "a:b"},
	} {
		parsed, ok := Parse(tc.target, tc.value)
		if !ok || parsed.Component != tc.component {
			t.Errorf("%#v on %q: got %#v", tc.target, tc.value, parsed)
			continue
		}
		rebuilt := parsed.Rebuild(secretValue)
		again, ok := Parse(tc.target, rebuilt)
		if rebuilt == tc.value || !ok || again.Component != secretValue {
			t.Errorf("%#v: the rebuild %q is not the inverse of the parse that found it", tc.target, rebuilt)
		}
	}
}

func TestAValueOfAnotherShapeIsNotThisTargets(t *testing.T) {
	for _, tc := range []struct {
		target resources.Target
		value  string
	}{
		{bearer, "abc"},
		{bearer, "Token abc"},
		{bearer, basicOf("a:b")},
		{basicUsername, "Bearer abc"},
		{basicUsername, basicOf("nocolon")},
		{basicPassword, basicOf("nocolon")},
		{basicWhole, "Basic not-base64!"},
		{basicWhole, "Basic " + base64.StdEncoding.EncodeToString([]byte{0xff, 0xfe})},
	} {
		if parsed, ok := Parse(tc.target, tc.value); ok {
			t.Errorf("%#v on %q: parsed %#v", tc.target, tc.value, parsed)
		}
	}
}

// The substring replace this design replaces would have spliced the real credential into every
// one of these and forwarded it.
func TestAComponentThatOnlyContainsThePlaceholderIsNotPresented(t *testing.T) {
	for _, value := range []string{
		"Bearer prefix" + placeholder, "Bearer " + placeholder + "suffix", "Bearer " + placeholder + " extra", placeholder,
	} {
		if _, ok := Present(credential("github-pat", bearer), http.Header{authorization: {value}}, nil); ok {
			t.Errorf("presented %q", value)
		}
	}
}

func TestThePlaceholderIsFoundAtWhicheverDeclaredTargetCarriesIt(t *testing.T) {
	github := credential("github-pat", bearer, basicPassword)

	byBearer, ok := Present(github, http.Header{authorization: {"Bearer " + placeholder}}, nil)
	want := []HeaderRewrite{{Header: authorization, Values: []string{"Bearer " + secretValue}}}
	if !ok || !reflect.DeepEqual(byBearer.Rewrites(secretValue), want) {
		t.Fatalf("got %#v", byBearer.Rewrites(secretValue))
	}
	byGit, ok := Present(github, http.Header{authorization: {basicOf("x-access-token:" + placeholder)}}, nil)
	want = []HeaderRewrite{{Header: authorization, Values: []string{basicOf("x-access-token:" + secretValue)}}}
	if !ok || !reflect.DeepEqual(byGit.Rewrites(secretValue), want) {
		t.Fatalf("got %#v", byGit.Rewrites(secretValue))
	}
}

func TestAHeaderNoTargetNamesIsNotLookedAt(t *testing.T) {
	if _, ok := Present(credential("github-pat", bearer), http.Header{"X-Other": {"Bearer " + placeholder}}, nil); ok {
		t.Fatal("presented")
	}
}

// A header sent twice is parsed per value: the one presenting the placeholder is rewritten and the
// other is forwarded exactly as it came.
func TestEveryValueOfARepeatedHeaderIsDecidedOnItsOwn(t *testing.T) {
	presentation, ok := Present(credential("github-pat", bearer),
		http.Header{authorization: {"Bearer other", "Bearer " + placeholder}}, nil)
	want := []HeaderRewrite{{Header: authorization, Values: []string{"Bearer other", "Bearer " + secretValue}}}
	if !ok || !reflect.DeepEqual(presentation.Rewrites(secretValue), want) {
		t.Fatalf("got %#v", presentation.Rewrites(secretValue))
	}
}

func TestACredentialPresentedInTwoHeadersIsRewrittenInBoth(t *testing.T) {
	presentation, ok := Present(credential("github-pat", bearer, whole),
		http.Header{authorization: {"Bearer " + placeholder}, "X-Api-Key": {placeholder}}, nil)
	want := []HeaderRewrite{
		{Header: authorization, Values: []string{"Bearer " + secretValue}},
		{Header: "X-Api-Key", Values: []string{secretValue}},
	}
	if !ok || !reflect.DeepEqual(presentation.Rewrites(secretValue), want) {
		t.Fatalf("got %#v", presentation.Rewrites(secretValue))
	}
}

// The forwarded request reads as the credential declares it, whatever case the client sent.
func TestTheRewrittenHeaderCarriesTheSpellingThePolicyDeclared(t *testing.T) {
	declared := resources.WholeValue{Header: "x-buildbuddy-api-key"}
	presentation, ok := Present(credential("github-pat", declared),
		http.Header{"X-Buildbuddy-Api-Key": {placeholder}, "X-Other": {"kept"}}, nil)
	if !ok {
		t.Fatal("not presented")
	}
	forwarded := http.Header{"X-Buildbuddy-Api-Key": {placeholder}, "X-Other": {"kept"}}
	for _, rewrite := range presentation.Rewrites(secretValue) {
		rewrite.Apply(forwarded)
	}
	want := http.Header{"x-buildbuddy-api-key": {secretValue}, "X-Other": {"kept"}}
	if !reflect.DeepEqual(forwarded, want) {
		t.Fatalf("got %#v", forwarded)
	}
}

var (
	jsonHeaders    = http.Header{"Content-Type": {"application/json; charset=utf-8"}}
	jsonCredential = credential("matrix-password", resources.JSONField{Field: "password"})
)

func TestJSONReplacementIsExactFieldOnlyAndEscaped(t *testing.T) {
	document := fmt.Sprintf(`{"password":%q,"identifier":{"user":"@bot:test"},"other":%q}`,
		jsonCredential.Placeholder(), jsonCredential.Placeholder())
	parsed, err := ParseJSONBody(jsonHeaders, []byte(document))
	if err != nil {
		t.Fatal(err)
	}
	presentation, ok := Present(jsonCredential, jsonHeaders, parsed)
	if !ok {
		t.Fatal("not presented")
	}
	value := "quotes\" and slash\\ and newline\n and <html> & Unicode café 😀"
	rewritten, err := presentation.BodyRewrite(value)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(rewritten.Content, &got); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{
		"password": value, "identifier": map[string]any{"user": "@bot:test"}, "other": jsonCredential.Placeholder(),
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
	if rewrites := presentation.Rewrites(value); len(rewrites) != 0 {
		t.Fatalf("a body credential rewrote headers: %#v", rewrites)
	}
	if strings.Contains(fmt.Sprint(rewritten, presentation), value) {
		t.Fatal("printed the credential")
	}
}

// Every byte outside the presented members reaches the upstream as it was sent.
func TestJSONRewriteLeavesEveryOtherByteAlone(t *testing.T) {
	parsed, err := ParseJSONBody(jsonHeaders,
		[]byte(fmt.Sprintf(`{"b":1.50, "password" : %q ,"a":[true,null,1e3,{"x":-0}], "z":"é"}`, jsonCredential.Placeholder())))
	if err != nil {
		t.Fatal(err)
	}
	presentation, _ := Present(jsonCredential, jsonHeaders, parsed)
	rewritten, err := presentation.BodyRewrite("v")
	if err != nil {
		t.Fatal(err)
	}
	if got := string(rewritten.Content); got != `{"b":1.50, "password" : "v" ,"a":[true,null,1e3,{"x":-0}], "z":"é"}` {
		t.Fatalf("got %s", got)
	}
}

func TestJSONRewriteRefusesACredentialThatIsNotAString(t *testing.T) {
	parsed, err := ParseJSONBody(jsonHeaders, []byte(fmt.Sprintf(`{"password":%q}`, jsonCredential.Placeholder())))
	if err != nil {
		t.Fatal(err)
	}
	presentation, _ := Present(jsonCredential, jsonHeaders, parsed)
	if _, err := presentation.BodyRewrite("not\xffutf8"); err == nil {
		t.Fatal("rewrote with bytes JSON cannot carry")
	}
}

func TestJSONSubstringsAndNonStringValuesAreNotPresentations(t *testing.T) {
	for _, value := range []string{
		`null`, `12`, `"prefix-` + jsonCredential.Placeholder() + `"`, `"` + jsonCredential.Placeholder() + `-suffix"`,
	} {
		parsed, err := ParseJSONBody(jsonHeaders, []byte(`{"password":`+value+`}`))
		if err != nil {
			t.Fatal(err)
		}
		if _, ok := Present(jsonCredential, jsonHeaders, parsed); ok {
			t.Errorf("presented %s", value)
		}
	}
	nested, err := ParseJSONBody(jsonHeaders, []byte(`{"nested":{"password":"`+jsonCredential.Placeholder()+`"}}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := Present(jsonCredential, jsonHeaders, nested); ok {
		t.Error("presented a nested field")
	}
}

func TestInvalidJSONIsRefusedWithoutParserInputInTheError(t *testing.T) {
	for name, body := range map[string][]byte{
		"duplicate key":        []byte(`{"password":"one","password":"two"}`),
		"nested duplicate":     []byte(`{"nested":{"x":1,"x":2}}`),
		"duplicate in array":   []byte(`{"a":[{"x":1,"x":2}]}`),
		"NaN":                  []byte(`{"x":NaN}`),
		"array":                []byte(`[]`),
		"null":                 []byte(`null`),
		"invalid":              []byte(`{invalid`),
		"invalid UTF-8":        []byte("{\"x\":\"\xff\"}"),
		"trailing data":        []byte(`{} {}`),
		"too large":            []byte(`{"x":"` + strings.Repeat("a", MaxJSONBodyBytes) + `"}`),
		"unread body":          nil,
		"non-string key token": []byte(`{1:2}`),
	} {
		_, err := ParseJSONBody(jsonHeaders, body)
		if !errors.Is(err, ErrInvalidJSONBody) || strings.Contains(err.Error(), "password") {
			t.Errorf("%s: got %v", name, err)
		}
	}
}

func TestUnsupportedJSONEnvelopesAreRefused(t *testing.T) {
	for name, headers := range map[string]http.Header{
		"none":              {},
		"text":              {"Content-Type": {"text/plain"}},
		"two content types": {"Content-Type": {"application/json", "application/json"}},
		"gzip":              {"Content-Type": {"application/json"}, "Content-Encoding": {"gzip"}},
	} {
		if _, err := ParseJSONBody(headers, []byte("{}")); !errors.Is(err, ErrInvalidJSONBody) {
			t.Errorf("%s: got %v", name, err)
		}
	}
	if _, err := ParseJSONBody(http.Header{"Content-Type": {"Application/JSON"}, "Content-Encoding": {"identity"}}, []byte("{}")); err != nil {
		t.Errorf("identity encoding is the same entity: %v", err)
	}
}
