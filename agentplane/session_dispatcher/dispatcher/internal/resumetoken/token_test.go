package resumetoken

import (
	"encoding/base64"
	"encoding/binary"
	"math"
	"strings"
	"testing"
)

func TestCheckpointRoundTripAndFilterBinding(t *testing.T) {
	digest := Digest([]byte("test-sessions:owner-a"))
	for _, version := range []int64{0, 1, 1 << 53, math.MaxInt64} {
		token := Encode(version, digest)
		got, err := Decode(token, digest)
		if err != nil || got != version {
			t.Fatalf("version %d decoded to %d: %v", version, got, err)
		}
		if _, err := Decode(token, Digest([]byte("test-dispatches:owner-a"))); err == nil {
			t.Fatal("accepted cursor from another filter/resource")
		}
	}
	if version, err := Decode("", digest); err != nil || version != 0 {
		t.Fatalf("fresh snapshot: %d %v", version, err)
	}
}

func TestRejectsUnsupportedOrMalformedTokens(t *testing.T) {
	digest := Digest([]byte("test-filter"))
	data, err := base64.RawURLEncoding.DecodeString(Encode(1, digest))
	if err != nil {
		t.Fatal(err)
	}
	unsupported := append([]byte(nil), data...)
	unsupported[0] = 2
	overflow := append([]byte(nil), data...)
	binary.BigEndian.PutUint64(overflow[1:9], math.MaxUint64)
	for _, token := range []string{"!", strings.Repeat("A", 10000), base64.RawURLEncoding.EncodeToString(unsupported), base64.RawURLEncoding.EncodeToString(overflow)} {
		if _, err := Decode(token, digest); err == nil {
			t.Fatalf("accepted malformed token %q", token[:1])
		}
	}
}
