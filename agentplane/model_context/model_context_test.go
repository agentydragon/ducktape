package modelcontext

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"testing"
)

func TestInternJSONCanonicalizesObjectsAndPreservesValues(t *testing.T) {
	graph := New()
	first, err := graph.InternJSON([]byte(`{"z":"雪\u0000","a":12345678901234567890123456789012345678901234567890,"list":[true,null,-12.5e+90]}`))
	if err != nil {
		t.Fatal(err)
	}
	second, err := graph.InternJSON([]byte(`{"list":[true,null,-12.5e+90],"a":12345678901234567890123456789012345678901234567890,"z":"雪\u0000"}`))
	if err != nil {
		t.Fatal(err)
	}
	if first != second {
		t.Fatalf("equivalent object IDs differ: %s != %s", first, second)
	}
	got, err := RenderJSON(first, graph)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"a":12345678901234567890123456789012345678901234567890,"list":[true,null,-12.5e+90],"z":"雪\u0000"}`
	if string(got) != want {
		t.Fatalf("RenderJSON() = %s, want %s", got, want)
	}
	if !json.Valid(got) {
		t.Fatalf("RenderJSON() produced invalid JSON: %s", got)
	}
}

func TestInternJSONRejectsDuplicateKeysTrailingAndInvalidInput(t *testing.T) {
	graph := New()
	for _, input := range []string{`{"a":1,"a":2}`, `1 true`, `01`, `"unterminated`, `"\ud800"`, `"\udc00"`, "\xff"} {
		t.Run(fmt.Sprintf("%x", []byte(input)), func(t *testing.T) {
			if _, err := graph.InternJSON([]byte(input)); err == nil {
				t.Fatalf("InternJSON(%q) unexpectedly succeeded", input)
			}
		})
	}
}

func TestSharedStringsUseOneBlobAcrossStructures(t *testing.T) {
	graph := New()
	shared := graph.String("same decoded payload")
	left, err := graph.Object(map[string]ID{"message": shared, "kind": graph.String("left")})
	if err != nil {
		t.Fatal(err)
	}
	right, err := graph.Object(map[string]ID{"message": shared, "kind": graph.String("right")})
	if err != nil {
		t.Fatal(err)
	}
	if left == right {
		t.Fatal("distinct objects unexpectedly share an ID")
	}
	snapshot := graph.Snapshot()
	matches := 0
	for _, blob := range snapshot.Blobs {
		if bytes.Equal(blob.Data, []byte("same decoded payload")) {
			matches++
		}
	}
	if matches != 1 {
		t.Fatalf("shared payload blob count = %d, want 1", matches)
	}
	if err := graph.Verify(); err != nil {
		t.Fatal(err)
	}
}

func TestPersistentSequenceBranchSliceConcatAndHydration(t *testing.T) {
	base := New()
	sequence, err := base.Sequence(nil)
	if err != nil {
		t.Fatal(err)
	}
	var firstValues []ID
	for _, value := range []string{"1", "2", "3"} {
		number, err := base.Number(value)
		if err != nil {
			t.Fatal(err)
		}
		sequence, err = base.Append(sequence, number)
		if err != nil {
			t.Fatal(err)
		}
		firstValues = append(firstValues, number)
	}

	branch := NewWithResolver(base)
	four, _ := branch.Number("4")
	branched, err := branch.Append(sequence, four)
	if err != nil {
		t.Fatal(err)
	}
	suffix, err := branch.Slice(sequence, 1, 3)
	if err != nil {
		t.Fatal(err)
	}
	combined, err := branch.Concat(suffix, branched)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := len(branch.Snapshot().Nodes), 4; got != want {
		t.Fatalf("branch snapshot node count = %d, want only 3 new nodes", got)
	}
	gotIDs, err := Elements(combined, branch)
	if err != nil {
		t.Fatal(err)
	}
	wantIDs := []ID{firstValues[1], firstValues[2], firstValues[0], firstValues[1], firstValues[2], four}
	if !equalIDs(gotIDs, wantIDs) {
		t.Fatalf("Elements() = %v, want %v", gotIDs, wantIDs)
	}
	got, err := RenderJSON(combined, branch)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != `[2,3,1,2,3,4]` {
		t.Fatalf("RenderJSON(sequence) = %s", got)
	}
	if err := branch.Verify(); err != nil {
		t.Fatal(err)
	}
}

func TestLegacyJSONPreservesObjectOrderAndJSONString(t *testing.T) {
	graph := New()
	legacy, err := graph.InternLegacyJSON([]byte(`{"second":2,"first":"x"}`))
	if err != nil {
		t.Fatal(err)
	}
	ordered, err := RenderJSON(legacy, graph)
	if err != nil {
		t.Fatal(err)
	}
	if string(ordered) != `{"second":2,"first":"x"}` {
		t.Fatalf("legacy render = %s", ordered)
	}
	fields, err := ObjectFields(legacy, graph)
	if err != nil {
		t.Fatal(err)
	}
	if len(fields) != 2 || fields[0].Name != "second" || fields[1].Name != "first" {
		t.Fatalf("ObjectFields() = %#v", fields)
	}
	encoded, err := graph.JSONString(legacy)
	if err != nil {
		t.Fatal(err)
	}
	wire, err := RenderJSON(encoded, graph)
	if err != nil {
		t.Fatal(err)
	}
	if string(wire) != `"{\"second\":2,\"first\":\"x\"}"` {
		t.Fatalf("JSONString render = %s", wire)
	}
}

func TestVerifyAndMissingDependency(t *testing.T) {
	graph := New()
	text := graph.String("value")
	if err := graph.Verify(); err != nil {
		t.Fatal(err)
	}

	badSnapshot := graph.Snapshot()
	for id := range badSnapshot.Blobs {
		delete(badSnapshot.Blobs, id)
	}
	incomplete := New()
	if err := incomplete.Merge(badSnapshot); err != nil {
		t.Fatal(err)
	}
	if err := incomplete.Verify(); !errors.Is(err, ErrNotFound) {
		t.Fatalf("Verify() error = %v, want ErrNotFound", err)
	}
	if _, err := RenderJSON(text, incomplete); !errors.Is(err, ErrNotFound) {
		t.Fatalf("RenderJSON() error = %v, want ErrNotFound", err)
	}

	baseSnapshot := graph.Snapshot()
	for id := range baseSnapshot.Blobs {
		delete(baseSnapshot.Blobs, id)
	}
	brokenBase := New()
	if err := brokenBase.Merge(baseSnapshot); err != nil {
		t.Fatal(err)
	}
	parent := NewWithResolver(graph)
	root, err := parent.Object(map[string]ID{"nested": text})
	if err != nil {
		t.Fatal(err)
	}
	transitive := NewWithResolver(brokenBase)
	if err := transitive.Merge(parent.Snapshot()); err != nil {
		t.Fatal(err)
	}
	if err := transitive.Verify(); !errors.Is(err, ErrNotFound) {
		t.Fatalf("transitive Verify() error = %v, want ErrNotFound", err)
	}
	if _, err := RenderJSON(root, parent); err != nil {
		t.Fatal(err)
	}

	missing, err := graph.Object(map[string]ID{"child": ID(strings.Repeat("a", 64))})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := RenderJSON(missing, graph); !errors.Is(err, ErrNotFound) {
		t.Fatalf("RenderJSON(missing child) error = %v, want ErrNotFound", err)
	}
}

func TestKindValidation(t *testing.T) {
	if !KindSequenceAppend.Valid() || KindSequenceAppend.Validate() != nil {
		t.Fatal("known sequence kind is not valid")
	}
	if Kind("unknown").Valid() || Kind("unknown").Validate() == nil {
		t.Fatal("unknown kind unexpectedly passed validation")
	}
}

func TestNodeAndBlobContentAddressVerification(t *testing.T) {
	graph := New()
	id := graph.String("original")
	node, err := graph.LoadNode(id)
	if err != nil {
		t.Fatal(err)
	}
	node.Kind = KindBytes
	if err := VerifyNode(id, node); !errors.Is(err, ErrInvalid) {
		t.Fatalf("VerifyNode() error = %v, want ErrInvalid", err)
	}
	for blobID, blob := range graph.Snapshot().Blobs {
		blob.Data = []byte("tampered")
		if err := VerifyBlob(blobID, blob); !errors.Is(err, ErrInvalid) {
			t.Fatalf("VerifyBlob() error = %v, want ErrInvalid", err)
		}
	}
}

func TestSequenceAppendStorageGrowthIsLinear(t *testing.T) {
	for _, size := range []int{100, 200, 400} {
		t.Run(strconv.Itoa(size), func(t *testing.T) {
			graph := New()
			sequence, err := graph.Sequence(nil)
			if err != nil {
				t.Fatal(err)
			}
			for i := 0; i < size; i++ {
				value := graph.String(fmt.Sprintf("entry-%03d", i))
				sequence, err = graph.Append(sequence, value)
				if err != nil {
					t.Fatal(err)
				}
			}
			snapshot := graph.Snapshot()
			if got, want := len(snapshot.Nodes), 2*size+1; got != want {
				t.Fatalf("node count = %d, want %d", got, want)
			}
			edges := 0
			for _, node := range snapshot.Nodes {
				edges += len(node.Children)
			}
			if got, want := edges, 3*size; got != want {
				t.Fatalf("reference count = %d, want %d", got, want)
			}
			if got, want := len(snapshot.Blobs), size; got != want {
				t.Fatalf("blob count = %d, want %d", got, want)
			}
			payloadBytes := 0
			for _, blob := range snapshot.Blobs {
				payloadBytes += len(blob.Data)
			}
			if got, want := payloadBytes, 9*size; got != want {
				t.Fatalf("payload bytes = %d, want %d", got, want)
			}
			if got, err := graph.Len(sequence); err != nil || got != size {
				t.Fatalf("Len() = %d, %v; want %d", got, err, size)
			}
			if _, err := RenderJSON(sequence, graph); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func equalIDs(left, right []ID) bool {
	if len(left) != len(right) {
		return false
	}
	for i := range left {
		if left[i] != right[i] {
			return false
		}
	}
	return true
}
