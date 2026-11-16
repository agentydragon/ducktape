// Package resumetoken encodes watch checkpoints bound to a canonical filter digest.
package resumetoken

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"math"
)

// Digest includes the resource kind, request filter and effective authorization scope.
// The caller supplies a canonical serialization so equivalent filters share a digest.
func Digest(filter []byte) [sha256.Size]byte { return sha256.Sum256(filter) }

func Encode(version int64, digest [sha256.Size]byte) string {
	var data [1 + 8 + sha256.Size]byte
	data[0] = 1
	binary.BigEndian.PutUint64(data[1:9], uint64(version))
	copy(data[9:], digest[:])
	return base64.RawURLEncoding.EncodeToString(data[:])
}

func Decode(token string, digest [sha256.Size]byte) (int64, error) {
	if token == "" {
		return 0, nil
	}
	if len(token) != base64.RawURLEncoding.EncodedLen(1+8+sha256.Size) {
		return 0, errors.New("invalid watch resume token")
	}
	data, err := base64.RawURLEncoding.DecodeString(token)
	if err != nil || data[0] != 1 {
		return 0, errors.New("invalid watch resume token")
	}
	if string(data[9:]) != string(digest[:]) {
		return 0, errors.New("watch resume token filter differs")
	}
	version := binary.BigEndian.Uint64(data[1:9])
	if version > math.MaxInt64 {
		return 0, errors.New("invalid watch resume version")
	}
	return int64(version), nil
}
