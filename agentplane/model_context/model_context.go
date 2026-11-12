// Package modelcontext stores immutable JSON values as a content-addressed DAG.
// String and byte payloads live in separate blobs so sharing a payload does not
// duplicate it in each parent node. Sequences are persistent DAG nodes whose
// append, slice, and concat operations retain references to their inputs.
package modelcontext

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// ID is a lowercase hexadecimal SHA-256 digest with a type-specific domain.
type ID string

// Kind identifies the structural form of a content-addressed node.
type Kind string

const (
	KindString         Kind = "string"
	KindBytes          Kind = "bytes"
	KindJSONString     Kind = "json_string"
	KindNumber         Kind = "number"
	KindBoolean        Kind = "boolean"
	KindNull           Kind = "null"
	KindObject         Kind = "object"
	KindOrderedObject  Kind = "ordered_object"
	KindArray          Kind = "array"
	KindSequence       Kind = "sequence"
	KindSequenceAppend Kind = "sequence_append"
	KindSequenceSlice  Kind = "sequence_slice"
	KindSequenceConcat Kind = "sequence_concat"
)

// Valid reports whether kind names a supported node shape.
func (kind Kind) Valid() bool {
	switch kind {
	case KindString, KindBytes, KindJSONString, KindNumber, KindBoolean, KindNull,
		KindObject, KindOrderedObject, KindArray, KindSequence, KindSequenceAppend,
		KindSequenceSlice, KindSequenceConcat:
		return true
	default:
		return false
	}
}

// Validate returns an error for an unsupported node kind.
func (kind Kind) Validate() error {
	if !kind.Valid() {
		return fmt.Errorf("%w: unknown node kind %q", ErrInvalid, kind)
	}
	return nil
}

// Node is an immutable JSON value or a persistent sequence operation. Data is
// a small, deterministic encoding for the node's scalar value or metadata;
// Children holds ordered references. Object children alternate key and value
// IDs, with keys sorted by their decoded UTF-8 bytes.
type Node struct {
	Kind     Kind
	Data     json.RawMessage
	Children []ID
}

// Blob holds a shared string or byte payload.
type Blob struct {
	ID   ID
	Data []byte
}

// Resolver loads immutable content by ID. Implementations may load from a
// database; returned values are verified before use.
type Resolver interface {
	LoadNode(ID) (Node, error)
	LoadBlob(ID) (Blob, error)
}

// Snapshot is a copy of the graph's locally-created nodes and blobs. Nodes
// loaded from a base Resolver are deliberately not included.
type Snapshot struct {
	Nodes map[ID]Node
	Blobs map[ID]Blob
}

// Field is an object member used when preserving an explicitly chosen order.
type Field struct {
	Name  string
	Value ID
}

// Graph accumulates immutable nodes and blobs. A base Resolver can provide
// existing content without copying it into Snapshot.
type Graph struct {
	nodes map[ID]Node
	blobs map[ID]Blob
	base  Resolver
}

var (
	ErrNotFound = errors.New("model context content not found")
	ErrInvalid  = errors.New("invalid model context content")
)

const (
	nodeDomain = "agentplane/model_context/node/v1\x00"
	blobDomain = "agentplane/model_context/blob/v1\x00"
)

// New returns an empty graph.
func New() *Graph {
	return &Graph{nodes: make(map[ID]Node), blobs: make(map[ID]Blob)}
}

// NewWithResolver returns an empty graph backed by base for existing content.
func NewWithResolver(base Resolver) *Graph {
	g := New()
	g.base = base
	return g
}

// SetResolver sets the fallback resolver used when an ID is absent locally.
func (g *Graph) SetResolver(base Resolver) {
	g.ensureMaps()
	g.base = base
}

func (g *Graph) ensureMaps() {
	if g.nodes == nil {
		g.nodes = make(map[ID]Node)
	}
	if g.blobs == nil {
		g.blobs = make(map[ID]Blob)
	}
}

// Clone copies locally-owned immutable records and keeps the same base resolver.
func (g *Graph) Clone() *Graph {
	clone := NewWithResolver(g.base)
	for id, node := range g.nodes {
		clone.nodes[id] = cloneNode(node)
	}
	for id, blob := range g.blobs {
		clone.blobs[id] = cloneBlob(blob)
	}
	return clone
}

// Snapshot returns a deep copy of locally-created records.
func (g *Graph) Snapshot() Snapshot {
	snapshot := Snapshot{
		Nodes: make(map[ID]Node, len(g.nodes)),
		Blobs: make(map[ID]Blob, len(g.blobs)),
	}
	for id, node := range g.nodes {
		snapshot.Nodes[id] = cloneNode(node)
	}
	for id, blob := range g.blobs {
		snapshot.Blobs[id] = cloneBlob(blob)
	}
	return snapshot
}

// Merge adds records from snapshot, verifying their content addresses and
// rejecting conflicting values. Child references may resolve through base.
func (g *Graph) Merge(snapshot Snapshot) error {
	g.ensureMaps()
	for id, blob := range snapshot.Blobs {
		if err := VerifyBlob(id, blob); err != nil {
			return err
		}
		if old, exists := g.blobs[id]; exists && !bytes.Equal(old.Data, blob.Data) {
			return fmt.Errorf("%w: conflicting blob %s", ErrInvalid, id)
		}
		g.blobs[id] = cloneBlob(blob)
	}
	for id, node := range snapshot.Nodes {
		if err := VerifyNode(id, node); err != nil {
			return err
		}
		if old, exists := g.nodes[id]; exists && !equalNode(old, node) {
			return fmt.Errorf("%w: conflicting node %s", ErrInvalid, id)
		}
		g.nodes[id] = cloneNode(node)
	}
	return nil
}

// LoadNode implements Resolver. Returned node data and children are copies.
func (g *Graph) LoadNode(id ID) (Node, error) {
	g.ensureMaps()
	if node, ok := g.nodes[id]; ok {
		if err := VerifyNode(id, node); err != nil {
			return Node{}, err
		}
		return cloneNode(node), nil
	}
	if g.base != nil {
		node, err := g.base.LoadNode(id)
		if err == nil {
			if verifyErr := VerifyNode(id, node); verifyErr != nil {
				return Node{}, verifyErr
			}
			return cloneNode(node), nil
		}
		if !errors.Is(err, ErrNotFound) {
			return Node{}, err
		}
	}
	return Node{}, fmt.Errorf("%w: node %s", ErrNotFound, id)
}

// LoadBlob implements Resolver. Returned payload bytes are copied.
func (g *Graph) LoadBlob(id ID) (Blob, error) {
	g.ensureMaps()
	if blob, ok := g.blobs[id]; ok {
		if err := VerifyBlob(id, blob); err != nil {
			return Blob{}, err
		}
		return cloneBlob(blob), nil
	}
	if g.base != nil {
		blob, err := g.base.LoadBlob(id)
		if err == nil {
			if verifyErr := VerifyBlob(id, blob); verifyErr != nil {
				return Blob{}, verifyErr
			}
			return cloneBlob(blob), nil
		}
		if !errors.Is(err, ErrNotFound) {
			return Blob{}, err
		}
	}
	return Blob{}, fmt.Errorf("%w: blob %s", ErrNotFound, id)
}

// Verify checks every locally-owned record and recursively resolves all
// dependencies, including records served by the base resolver.
func (g *Graph) Verify() error {
	for id, blob := range g.blobs {
		if err := VerifyBlob(id, blob); err != nil {
			return err
		}
	}
	stack := make([]ID, 0, len(g.nodes))
	for id := range g.nodes {
		stack = append(stack, id)
	}
	seenNodes := make(map[ID]struct{})
	seenBlobs := make(map[ID]struct{})
	for len(stack) > 0 {
		id := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if _, seen := seenNodes[id]; seen {
			continue
		}
		node, err := g.LoadNode(id)
		if err != nil {
			return err
		}
		seenNodes[id] = struct{}{}
		if node.Kind == KindString || node.Kind == KindBytes {
			blobID := node.Children[0]
			if _, seen := seenBlobs[blobID]; seen {
				continue
			}
			if _, err := g.LoadBlob(blobID); err != nil {
				return fmt.Errorf("node %s depends on blob %s: %w", id, blobID, err)
			}
			seenBlobs[blobID] = struct{}{}
			continue
		}
		stack = append(stack, node.Children...)
	}
	return nil
}

// VerifyNode verifies a node's structure and content address.
func VerifyNode(id ID, node Node) error {
	if !validID(id) {
		return fmt.Errorf("%w: malformed node ID %q", ErrInvalid, id)
	}
	if err := validateNodeShape(node); err != nil {
		return err
	}
	for _, child := range node.Children {
		if !validID(child) {
			return fmt.Errorf("%w: malformed child ID %q", ErrInvalid, child)
		}
	}
	if got := hashNode(node); got != id {
		return fmt.Errorf("%w: node hash mismatch for %s (computed %s)", ErrInvalid, id, got)
	}
	return nil
}

// VerifyBlob verifies a blob's ID and payload.
func VerifyBlob(id ID, blob Blob) error {
	if !validID(id) || blob.ID != id {
		return fmt.Errorf("%w: malformed or mismatched blob ID %q", ErrInvalid, id)
	}
	if got := hashBlob(blob.Data); got != id {
		return fmt.Errorf("%w: blob hash mismatch for %s (computed %s)", ErrInvalid, id, got)
	}
	return nil
}

// String interns a JSON string whose UTF-8 payload is stored once as a Blob.
func (g *Graph) String(value string) ID {
	blobID := g.internBlob([]byte(value))
	return g.internNode(Node{Kind: KindString, Children: []ID{blobID}})
}

// Bytes interns bytes as a JSON base64 string node backed by a Blob.
func (g *Graph) Bytes(value []byte) ID {
	blobID := g.internBlob(value)
	return g.internNode(Node{Kind: KindBytes, Children: []ID{blobID}})
}

// JSONString renders value as JSON and stores that final JSON encoding as a
// JSON string at render time. It keeps a reference to value instead of storing
// a second, potentially large wire-request payload.
func (g *Graph) JSONString(value ID) (ID, error) {
	if !validID(value) {
		return "", fmt.Errorf("%w: malformed JSON string value ID %q", ErrInvalid, value)
	}
	return g.internNode(Node{Kind: KindJSONString, Children: []ID{value}}), nil
}

// PutNode validates and interns a node with caller-supplied kind, data, and
// ordered child references.
func (g *Graph) PutNode(node Node) (ID, error) {
	if err := validateNodeShape(node); err != nil {
		return "", err
	}
	for _, child := range node.Children {
		if !validID(child) {
			return "", fmt.Errorf("%w: malformed child ID %q", ErrInvalid, child)
		}
	}
	return g.internNode(node), nil
}

// Number interns a JSON number without converting it through floating point.
func (g *Graph) Number(value string) (ID, error) {
	if err := validateNumber([]byte(value)); err != nil {
		return "", err
	}
	return g.internNode(Node{Kind: KindNumber, Data: json.RawMessage(value)}), nil
}

// Bool interns a JSON boolean.
func (g *Graph) Bool(value bool) ID {
	data := json.RawMessage("false")
	if value {
		data = json.RawMessage("true")
	}
	return g.internNode(Node{Kind: KindBoolean, Data: data})
}

// Null interns JSON null.
func (g *Graph) Null() ID {
	return g.internNode(Node{Kind: KindNull})
}

// Object interns fields with keys sorted deterministically. Child edges
// alternate key string IDs and value IDs.
func (g *Graph) Object(fields map[string]ID) (ID, error) {
	keys := make([]string, 0, len(fields))
	for key, value := range fields {
		if !utf8.ValidString(key) {
			return "", fmt.Errorf("%w: object key is not valid UTF-8", ErrInvalid)
		}
		if !validID(value) {
			return "", fmt.Errorf("%w: malformed object value ID %q", ErrInvalid, value)
		}
		keys = append(keys, key)
	}
	sort.Strings(keys)
	children := make([]ID, 0, len(keys)*2)
	for _, key := range keys {
		children = append(children, g.String(key), fields[key])
	}
	return g.internNode(Node{Kind: KindObject, Children: children}), nil
}

// OrderedObject interns object fields in the supplied order. Field names must
// be unique; use Object when deterministic key sorting is preferred.
func (g *Graph) OrderedObject(fields []Field) (ID, error) {
	children := make([]ID, 0, len(fields)*2)
	seen := make(map[string]struct{}, len(fields))
	for _, field := range fields {
		if !utf8.ValidString(field.Name) || !validID(field.Value) {
			return "", fmt.Errorf("%w: invalid ordered object field %q", ErrInvalid, field.Name)
		}
		if _, exists := seen[field.Name]; exists {
			return "", fmt.Errorf("%w: duplicate ordered object key %q", ErrInvalid, field.Name)
		}
		seen[field.Name] = struct{}{}
		children = append(children, g.String(field.Name), field.Value)
	}
	return g.internNode(Node{Kind: KindOrderedObject, Children: children}), nil
}

// Array interns an ordered JSON array.
func (g *Graph) Array(values []ID) (ID, error) {
	children := append([]ID(nil), values...)
	for _, value := range children {
		if !validID(value) {
			return "", fmt.Errorf("%w: malformed array value ID %q", ErrInvalid, value)
		}
	}
	return g.internNode(Node{Kind: KindArray, Children: children}), nil
}

// Sequence creates a persistent JSON-array sequence from values.
func (g *Graph) Sequence(values []ID) (ID, error) {
	children := append([]ID(nil), values...)
	for _, value := range children {
		if !validID(value) {
			return "", fmt.Errorf("%w: malformed sequence value ID %q", ErrInvalid, value)
		}
	}
	return g.internNode(Node{Kind: KindSequence, Data: countData(len(children)), Children: children}), nil
}

// Len returns a persistent sequence's element count.
func (g *Graph) Len(sequence ID) (int, error) {
	node, err := g.LoadNode(sequence)
	if err != nil {
		return 0, err
	}
	if err := validateNodeShape(node); err != nil {
		return 0, err
	}
	if !isSequenceKind(node.Kind) {
		return 0, fmt.Errorf("%w: %s is not a sequence", ErrInvalid, sequence)
	}
	return sequenceCount(node)
}

// Append returns a sequence with value appended, keeping the old sequence as a
// single reference so appending does not copy the prefix.
func (g *Graph) Append(sequence, value ID) (ID, error) {
	if !validID(value) {
		return "", fmt.Errorf("%w: malformed sequence value ID %q", ErrInvalid, value)
	}
	length, err := g.Len(sequence)
	if err != nil {
		return "", err
	}
	if length == math.MaxInt {
		return "", fmt.Errorf("%w: sequence length overflow", ErrInvalid)
	}
	return g.internNode(Node{Kind: KindSequenceAppend, Data: countData(length + 1), Children: []ID{sequence, value}}), nil
}

// Slice returns sequence[start:end] as a view, without copying element refs.
func (g *Graph) Slice(sequence ID, start, end int) (ID, error) {
	length, err := g.Len(sequence)
	if err != nil {
		return "", err
	}
	if start < 0 || end < start || end > length {
		return "", fmt.Errorf("%w: invalid sequence slice [%d:%d] for length %d", ErrInvalid, start, end, length)
	}
	data, _ := json.Marshal(struct {
		Start int `json:"start"`
		Count int `json:"count"`
	}{Start: start, Count: end - start})
	return g.internNode(Node{Kind: KindSequenceSlice, Data: data, Children: []ID{sequence}}), nil
}

// Concat returns a sequence view over left followed by right.
func (g *Graph) Concat(left, right ID) (ID, error) {
	leftLen, err := g.Len(left)
	if err != nil {
		return "", err
	}
	rightLen, err := g.Len(right)
	if err != nil {
		return "", err
	}
	if rightLen > math.MaxInt-leftLen {
		return "", fmt.Errorf("%w: sequence length overflow", ErrInvalid)
	}
	return g.internNode(Node{Kind: KindSequenceConcat, Data: countData(leftLen + rightLen), Children: []ID{left, right}}), nil
}

// InternJSON parses strict JSON and interns its semantic value. Object keys are
// sorted; array order, strings, and number lexemes are preserved. Duplicate
// keys, trailing values, invalid UTF-8, and malformed JSON are rejected.
func (g *Graph) InternJSON(input []byte) (ID, error) {
	if err := validateJSONInput(input); err != nil {
		return "", err
	}
	decoder := json.NewDecoder(bytes.NewReader(input))
	decoder.UseNumber()
	parser := jsonParser{decoder: decoder, graph: g}
	id, err := parser.value()
	if err != nil {
		return "", err
	}
	if _, err := decoder.Token(); err != io.EOF {
		if err == nil {
			return "", fmt.Errorf("%w: trailing JSON value", ErrInvalid)
		}
		return "", fmt.Errorf("%w: trailing JSON data: %v", ErrInvalid, err)
	}
	return id, nil
}

// InternLegacyJSON interns compact historical JSON while retaining encountered
// object key order. It still rejects duplicate keys, trailing values, invalid
// UTF-8, and malformed JSON.
func (g *Graph) InternLegacyJSON(input []byte) (ID, error) {
	if err := validateJSONInput(input); err != nil {
		return "", err
	}
	decoder := json.NewDecoder(bytes.NewReader(input))
	decoder.UseNumber()
	parser := jsonParser{decoder: decoder, graph: g, preserveOrder: true}
	id, err := parser.value()
	if err != nil {
		return "", err
	}
	if _, err := decoder.Token(); err != io.EOF {
		if err == nil {
			return "", fmt.Errorf("%w: trailing JSON value", ErrInvalid)
		}
		return "", fmt.Errorf("%w: trailing JSON data: %v", ErrInvalid, err)
	}
	return id, nil
}

type jsonParser struct {
	decoder       *json.Decoder
	graph         *Graph
	preserveOrder bool
}

func validateJSONInput(input []byte) error {
	if !utf8.Valid(input) {
		return fmt.Errorf("%w: JSON input is not valid UTF-8", ErrInvalid)
	}
	for i := 0; i < len(input); {
		if input[i] != '"' {
			i++
			continue
		}
		i++
		for i < len(input) {
			switch input[i] {
			case '"':
				i++
				goto nextString
			case '\\':
				if i+1 >= len(input) {
					return fmt.Errorf("%w: incomplete JSON string escape", ErrInvalid)
				}
				if input[i+1] != 'u' {
					i += 2
					continue
				}
				if i+6 > len(input) {
					return fmt.Errorf("%w: incomplete JSON unicode escape", ErrInvalid)
				}
				code, err := strconv.ParseUint(string(input[i+2:i+6]), 16, 16)
				if err != nil {
					return fmt.Errorf("%w: invalid JSON unicode escape", ErrInvalid)
				}
				if code >= 0xD800 && code <= 0xDBFF {
					next := i + 6
					if next+6 > len(input) || input[next] != '\\' || input[next+1] != 'u' {
						return fmt.Errorf("%w: unpaired high surrogate in JSON string", ErrInvalid)
					}
					low, err := strconv.ParseUint(string(input[next+2:next+6]), 16, 16)
					if err != nil || low < 0xDC00 || low > 0xDFFF {
						return fmt.Errorf("%w: unpaired high surrogate in JSON string", ErrInvalid)
					}
					i = next + 6
					continue
				}
				if code >= 0xDC00 && code <= 0xDFFF {
					return fmt.Errorf("%w: unpaired low surrogate in JSON string", ErrInvalid)
				}
				i += 6
			default:
				i++
			}
		}
		break
	nextString:
	}
	return nil
}

func (p *jsonParser) value() (ID, error) {
	token, err := p.decoder.Token()
	if err != nil {
		return "", fmt.Errorf("%w: decode JSON: %v", ErrInvalid, err)
	}
	switch value := token.(type) {
	case nil:
		return p.graph.Null(), nil
	case bool:
		return p.graph.Bool(value), nil
	case string:
		return p.graph.String(value), nil
	case json.Number:
		return p.graph.Number(value.String())
	case json.Delim:
		switch value {
		case '{':
			seen := make(map[string]struct{})
			fields := make([]Field, 0)
			for p.decoder.More() {
				keyToken, err := p.decoder.Token()
				if err != nil {
					return "", fmt.Errorf("%w: decode object key: %v", ErrInvalid, err)
				}
				key, ok := keyToken.(string)
				if !ok {
					return "", fmt.Errorf("%w: object key has type %T", ErrInvalid, keyToken)
				}
				if _, duplicate := seen[key]; duplicate {
					return "", fmt.Errorf("%w: duplicate object key %q", ErrInvalid, key)
				}
				seen[key] = struct{}{}
				child, err := p.value()
				if err != nil {
					return "", err
				}
				fields = append(fields, Field{Name: key, Value: child})
			}
			if _, err := p.decoder.Token(); err != nil {
				return "", fmt.Errorf("%w: close object: %v", ErrInvalid, err)
			}
			if p.preserveOrder {
				return p.graph.OrderedObject(fields)
			}
			fieldMap := make(map[string]ID, len(fields))
			for _, field := range fields {
				fieldMap[field.Name] = field.Value
			}
			return p.graph.Object(fieldMap)
		case '[':
			values := make([]ID, 0)
			for p.decoder.More() {
				child, err := p.value()
				if err != nil {
					return "", err
				}
				values = append(values, child)
			}
			if _, err := p.decoder.Token(); err != nil {
				return "", fmt.Errorf("%w: close array: %v", ErrInvalid, err)
			}
			return p.graph.Array(values)
		default:
			return "", fmt.Errorf("%w: unexpected JSON delimiter %q", ErrInvalid, value)
		}
	default:
		return "", fmt.Errorf("%w: unexpected JSON token %T", ErrInvalid, token)
	}
}

func isSequenceKind(kind Kind) bool {
	switch kind {
	case KindSequence, KindSequenceAppend, KindSequenceSlice, KindSequenceConcat:
		return true
	default:
		return false
	}
}

func sequenceCount(node Node) (int, error) {
	if err := validateNodeShape(node); err != nil {
		return 0, err
	}
	if node.Kind == KindSequenceSlice {
		meta, err := decodeSliceData(node.Data)
		return meta.Count, err
	}
	return decodeCount(node.Data)
}

// ObjectFields returns object members in their stored order.
func ObjectFields(id ID, resolver Resolver) ([]Field, error) {
	renderer := newRenderer(resolver)
	node, err := renderer.loadNode(id)
	if err != nil {
		return nil, err
	}
	if node.Kind != KindObject && node.Kind != KindOrderedObject {
		return nil, fmt.Errorf("%w: %s is not an object", ErrInvalid, id)
	}
	fields := make([]Field, 0, len(node.Children)/2)
	for i := 0; i < len(node.Children); i += 2 {
		keyNode, err := renderer.loadNode(node.Children[i])
		if err != nil {
			return nil, err
		}
		if keyNode.Kind != KindString {
			return nil, fmt.Errorf("%w: object key %s is not a string", ErrInvalid, node.Children[i])
		}
		blob, err := renderer.loadBlob(keyNode.Children[0])
		if err != nil {
			return nil, err
		}
		fields = append(fields, Field{Name: string(blob.Data), Value: node.Children[i+1]})
	}
	return fields, nil
}

func (g *Graph) internBlob(data []byte) ID {
	g.ensureMaps()
	id := hashBlob(data)
	if _, ok := g.blobs[id]; !ok {
		dataCopy := make([]byte, len(data))
		copy(dataCopy, data)
		g.blobs[id] = Blob{ID: id, Data: dataCopy}
	}
	return id
}

func (g *Graph) internNode(node Node) ID {
	g.ensureMaps()
	node = cloneNode(node)
	id := hashNode(node)
	if _, ok := g.nodes[id]; !ok {
		g.nodes[id] = node
	}
	return id
}

func validateNodeShape(node Node) error {
	invalid := func(reason string) error { return fmt.Errorf("%w: %s node: %s", ErrInvalid, node.Kind, reason) }
	if err := node.Kind.Validate(); err != nil {
		return err
	}
	switch node.Kind {
	case KindString, KindBytes:
		if len(node.Data) != 0 || len(node.Children) != 1 {
			return invalid("requires one blob child and no data")
		}
	case KindJSONString:
		if len(node.Data) != 0 || len(node.Children) != 1 {
			return invalid("requires one value child and no data")
		}
	case KindNumber:
		if len(node.Children) != 0 || validateNumber(node.Data) != nil {
			return invalid("requires a valid JSON number and no children")
		}
	case KindBoolean:
		if len(node.Children) != 0 || (string(node.Data) != "true" && string(node.Data) != "false") {
			return invalid("requires true or false and no children")
		}
	case KindNull:
		if len(node.Data) != 0 || len(node.Children) != 0 {
			return invalid("must have no data or children")
		}
	case KindObject, KindOrderedObject:
		if len(node.Data) != 0 || len(node.Children)%2 != 0 {
			return invalid("requires alternating key and value children")
		}
	case KindArray:
		if len(node.Data) != 0 {
			return invalid("must have no data")
		}
	case KindSequence:
		count, err := decodeCount(node.Data)
		if err != nil || count != len(node.Children) {
			return invalid("count must equal the number of children")
		}
	case KindSequenceAppend:
		count, err := decodeCount(node.Data)
		if err != nil || count == 0 || len(node.Children) != 2 {
			return invalid("requires positive count and two children")
		}
	case KindSequenceSlice:
		meta, err := decodeSliceData(node.Data)
		if err != nil || meta.Count < 0 || meta.Start < 0 || len(node.Children) != 1 {
			return invalid("requires valid range metadata and one child")
		}
	case KindSequenceConcat:
		if _, err := decodeCount(node.Data); err != nil || len(node.Children) != 2 {
			return invalid("requires count metadata and two children")
		}
	default:
		return invalid("unknown kind")
	}
	return nil
}

func validateNumber(raw []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	token, err := decoder.Token()
	if err != nil {
		return fmt.Errorf("%w: invalid JSON number %q: %v", ErrInvalid, raw, err)
	}
	number, ok := token.(json.Number)
	if !ok || number.String() != string(raw) {
		return fmt.Errorf("%w: %q is not a canonical JSON number token", ErrInvalid, raw)
	}
	if _, err := decoder.Token(); err != io.EOF {
		return fmt.Errorf("%w: trailing bytes in number %q", ErrInvalid, raw)
	}
	return nil
}

type sliceData struct {
	Start int `json:"start"`
	Count int `json:"count"`
}

func decodeSliceData(data []byte) (sliceData, error) {
	var meta sliceData
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&meta); err != nil {
		return sliceData{}, fmt.Errorf("%w: invalid sequence slice metadata: %v", ErrInvalid, err)
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return sliceData{}, fmt.Errorf("%w: trailing sequence slice metadata", ErrInvalid)
	}
	canonical, _ := json.Marshal(meta)
	if !bytes.Equal(data, canonical) || meta.Start < 0 || meta.Count < 0 {
		return sliceData{}, fmt.Errorf("%w: noncanonical sequence slice metadata", ErrInvalid)
	}
	return meta, nil
}

func countData(count int) json.RawMessage {
	return json.RawMessage(strconv.Itoa(count))
}

func decodeCount(data []byte) (int, error) {
	if len(data) == 0 || strings.TrimSpace(string(data)) != string(data) || (len(data) > 1 && data[0] == '0') {
		return 0, fmt.Errorf("%w: invalid sequence count %q", ErrInvalid, data)
	}
	count, err := strconv.Atoi(string(data))
	if err != nil || count < 0 || strconv.Itoa(count) != string(data) {
		return 0, fmt.Errorf("%w: invalid sequence count %q", ErrInvalid, data)
	}
	return count, nil
}

func hashNode(node Node) ID {
	hash := sha256.New()
	_, _ = io.WriteString(hash, nodeDomain)
	writeUint64(hash, uint64(len(node.Kind)))
	_, _ = io.WriteString(hash, string(node.Kind))
	writeUint64(hash, uint64(len(node.Data)))
	_, _ = hash.Write(node.Data)
	writeUint64(hash, uint64(len(node.Children)))
	for _, child := range node.Children {
		_, _ = io.WriteString(hash, string(child))
	}
	return ID(hex.EncodeToString(hash.Sum(nil)))
}

func hashBlob(data []byte) ID {
	hash := sha256.New()
	_, _ = io.WriteString(hash, blobDomain)
	writeUint64(hash, uint64(len(data)))
	_, _ = hash.Write(data)
	return ID(hex.EncodeToString(hash.Sum(nil)))
}

func writeUint64(writer io.Writer, value uint64) {
	var buffer [8]byte
	binary.BigEndian.PutUint64(buffer[:], value)
	_, _ = writer.Write(buffer[:])
}

func validID(id ID) bool {
	if len(id) != sha256.Size*2 {
		return false
	}
	decoded, err := hex.DecodeString(string(id))
	return err == nil && hex.EncodeToString(decoded) == string(id)
}

func cloneNode(node Node) Node {
	data := make(json.RawMessage, len(node.Data))
	copy(data, node.Data)
	children := make([]ID, len(node.Children))
	copy(children, node.Children)
	return Node{Kind: node.Kind, Data: data, Children: children}
}

func cloneBlob(blob Blob) Blob {
	data := make([]byte, len(blob.Data))
	copy(data, blob.Data)
	return Blob{ID: blob.ID, Data: data}
}

func equalNode(left, right Node) bool {
	if left.Kind != right.Kind || !bytes.Equal(left.Data, right.Data) || len(left.Children) != len(right.Children) {
		return false
	}
	for i := range left.Children {
		if left.Children[i] != right.Children[i] {
			return false
		}
	}
	return true
}
