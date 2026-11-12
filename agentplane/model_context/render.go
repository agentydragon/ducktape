package modelcontext

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"unicode/utf8"
)

const maxJSONDepth = 4096

// RenderJSON resolves and verifies id, then writes its JSON representation.
// Each referenced node/blob is loaded at most once per render operation.
func RenderJSON(id ID, resolver Resolver) ([]byte, error) {
	renderer := newRenderer(resolver)
	var output bytes.Buffer
	if err := renderer.renderValue(id, &output, 0); err != nil {
		return nil, err
	}
	return output.Bytes(), nil
}

// Elements returns the item IDs in a persistent sequence without rendering or
// reinterning them. Append chains are traversed iteratively to bound stack use.
func Elements(id ID, resolver Resolver) ([]ID, error) {
	renderer := newRenderer(resolver)
	return renderer.sequenceElements(id)
}

// Elements returns this graph's persistent sequence item IDs.
func (g *Graph) Elements(id ID) ([]ID, error) {
	return Elements(id, g)
}

type renderer struct {
	resolver Resolver
	nodes    map[ID]Node
	blobs    map[ID]Blob
	active   map[ID]bool
}

func newRenderer(resolver Resolver) *renderer {
	return &renderer{
		resolver: resolver,
		nodes:    make(map[ID]Node),
		blobs:    make(map[ID]Blob),
		active:   make(map[ID]bool),
	}
}

func (r *renderer) loadNode(id ID) (Node, error) {
	if node, ok := r.nodes[id]; ok {
		return node, nil
	}
	if r.resolver == nil {
		return Node{}, fmt.Errorf("%w: node %s (no resolver)", ErrNotFound, id)
	}
	node, err := r.resolver.LoadNode(id)
	if err != nil {
		return Node{}, err
	}
	if err := VerifyNode(id, node); err != nil {
		return Node{}, err
	}
	node = cloneNode(node)
	r.nodes[id] = node
	return node, nil
}

func (r *renderer) loadBlob(id ID) (Blob, error) {
	if blob, ok := r.blobs[id]; ok {
		return blob, nil
	}
	if r.resolver == nil {
		return Blob{}, fmt.Errorf("%w: blob %s (no resolver)", ErrNotFound, id)
	}
	blob, err := r.resolver.LoadBlob(id)
	if err != nil {
		return Blob{}, err
	}
	if err := VerifyBlob(id, blob); err != nil {
		return Blob{}, err
	}
	blob = cloneBlob(blob)
	r.blobs[id] = blob
	return blob, nil
}

func (r *renderer) renderValue(id ID, output *bytes.Buffer, depth int) error {
	if depth > maxJSONDepth {
		return fmt.Errorf("%w: JSON nesting exceeds %d", ErrInvalid, maxJSONDepth)
	}
	if r.active[id] {
		return fmt.Errorf("%w: node cycle at %s", ErrInvalid, id)
	}
	node, err := r.loadNode(id)
	if err != nil {
		return err
	}
	r.active[id] = true
	defer delete(r.active, id)

	switch node.Kind {
	case KindString:
		blob, err := r.loadBlob(node.Children[0])
		if err != nil {
			return err
		}
		if !utf8.Valid(blob.Data) {
			return fmt.Errorf("%w: string blob %s is not valid UTF-8", ErrInvalid, blob.ID)
		}
		encoded, _ := json.Marshal(string(blob.Data))
		output.Write(encoded)
	case KindBytes:
		blob, err := r.loadBlob(node.Children[0])
		if err != nil {
			return err
		}
		encoded, _ := json.Marshal(base64.StdEncoding.EncodeToString(blob.Data))
		output.Write(encoded)
	case KindJSONString:
		var value bytes.Buffer
		if err := r.renderValue(node.Children[0], &value, depth+1); err != nil {
			return err
		}
		encoded, err := json.Marshal(value.String())
		if err != nil {
			return fmt.Errorf("%w: encode JSON string: %v", ErrInvalid, err)
		}
		output.Write(encoded)
	case KindNumber, KindBoolean:
		output.Write(node.Data)
	case KindNull:
		output.WriteString("null")
	case KindObject, KindOrderedObject:
		if err := r.renderObject(node, output, depth); err != nil {
			return err
		}
	case KindArray:
		output.WriteByte('[')
		for i, child := range node.Children {
			if i > 0 {
				output.WriteByte(',')
			}
			if err := r.renderValue(child, output, depth+1); err != nil {
				return err
			}
		}
		output.WriteByte(']')
	case KindSequence, KindSequenceAppend, KindSequenceSlice, KindSequenceConcat:
		values, err := r.sequenceElements(id)
		if err != nil {
			return err
		}
		output.WriteByte('[')
		for i, child := range values {
			if i > 0 {
				output.WriteByte(',')
			}
			if err := r.renderValue(child, output, depth+1); err != nil {
				return err
			}
		}
		output.WriteByte(']')
	default:
		return fmt.Errorf("%w: unknown node kind %q", ErrInvalid, node.Kind)
	}
	return nil
}

func (r *renderer) renderObject(node Node, output *bytes.Buffer, depth int) error {
	output.WriteByte('{')
	seen := make(map[string]struct{}, len(node.Children)/2)
	previous := ""
	for i := 0; i < len(node.Children); i += 2 {
		keyNode, err := r.loadNode(node.Children[i])
		if err != nil {
			return err
		}
		if keyNode.Kind != KindString {
			return fmt.Errorf("%w: object key %s is not a string node", ErrInvalid, node.Children[i])
		}
		blob, err := r.loadBlob(keyNode.Children[0])
		if err != nil {
			return err
		}
		if !utf8.Valid(blob.Data) {
			return fmt.Errorf("%w: object key blob %s is not valid UTF-8", ErrInvalid, blob.ID)
		}
		key := string(blob.Data)
		if _, exists := seen[key]; exists {
			return fmt.Errorf("%w: duplicate object key %q", ErrInvalid, key)
		}
		if node.Kind == KindObject && i > 0 && key <= previous {
			return fmt.Errorf("%w: object keys are not sorted", ErrInvalid)
		}
		seen[key] = struct{}{}
		previous = key
		if i > 0 {
			output.WriteByte(',')
		}
		encodedKey, _ := json.Marshal(key)
		output.Write(encodedKey)
		output.WriteByte(':')
		if err := r.renderValue(node.Children[i+1], output, depth+1); err != nil {
			return err
		}
	}
	output.WriteByte('}')
	return nil
}

type sequenceTask struct {
	id    ID
	start int
	count int
	value bool
	exit  bool
}

func (r *renderer) sequenceElements(id ID) ([]ID, error) {
	root, err := r.loadNode(id)
	if err != nil {
		return nil, err
	}
	count, err := sequenceNodeCount(root)
	if err != nil {
		return nil, err
	}
	stack := []sequenceTask{{id: id, start: 0, count: count}}
	active := make(map[ID]bool)
	values := make([]ID, 0, count)
	for len(stack) > 0 {
		task := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if task.value {
			values = append(values, task.id)
			continue
		}
		if task.exit {
			delete(active, task.id)
			continue
		}
		if active[task.id] {
			return nil, fmt.Errorf("%w: sequence cycle at %s", ErrInvalid, task.id)
		}
		active[task.id] = true
		node, err := r.loadNode(task.id)
		if err != nil {
			return nil, err
		}
		total, err := sequenceNodeCount(node)
		if err != nil {
			return nil, err
		}
		if task.start < 0 || task.count < 0 || task.start > total || task.count > total-task.start {
			return nil, fmt.Errorf("%w: invalid sequence range [%d:%d] for length %d", ErrInvalid, task.start, task.start+task.count, total)
		}
		stack = append(stack, sequenceTask{id: task.id, exit: true})
		end := task.start + task.count
		switch node.Kind {
		case KindSequence:
			for i := end - 1; i >= task.start; i-- {
				stack = append(stack, sequenceTask{id: node.Children[i], value: true})
			}
		case KindSequenceAppend:
			previous, err := r.loadNode(node.Children[0])
			if err != nil {
				return nil, err
			}
			previousCount, err := sequenceNodeCount(previous)
			if err != nil || previousCount+1 != total {
				return nil, fmt.Errorf("%w: append count mismatch for %s", ErrInvalid, task.id)
			}
			if task.start <= previousCount && end > previousCount {
				stack = append(stack, sequenceTask{id: node.Children[1], value: true})
			}
			prefixEnd := end
			if prefixEnd > previousCount {
				prefixEnd = previousCount
			}
			if prefixEnd > task.start {
				stack = append(stack, sequenceTask{id: node.Children[0], start: task.start, count: prefixEnd - task.start})
			}
		case KindSequenceSlice:
			meta, err := decodeSliceData(node.Data)
			if err != nil || meta.Count != total {
				return nil, fmt.Errorf("%w: malformed slice metadata for %s", ErrInvalid, task.id)
			}
			base, err := r.loadNode(node.Children[0])
			if err != nil {
				return nil, err
			}
			baseCount, err := sequenceNodeCount(base)
			if err != nil || meta.Start > baseCount || meta.Count > baseCount-meta.Start {
				return nil, fmt.Errorf("%w: slice bounds mismatch for %s", ErrInvalid, task.id)
			}
			stack = append(stack, sequenceTask{id: node.Children[0], start: meta.Start + task.start, count: task.count})
		case KindSequenceConcat:
			left, err := r.loadNode(node.Children[0])
			if err != nil {
				return nil, err
			}
			leftCount, err := sequenceNodeCount(left)
			if err != nil {
				return nil, err
			}
			right, err := r.loadNode(node.Children[1])
			if err != nil {
				return nil, err
			}
			rightCount, err := sequenceNodeCount(right)
			if err != nil || rightCount > int(^uint(0)>>1)-leftCount || leftCount+rightCount != total {
				return nil, fmt.Errorf("%w: concat count mismatch for %s", ErrInvalid, task.id)
			}
			leftEnd := end
			if leftEnd > leftCount {
				leftEnd = leftCount
			}
			if end > leftCount {
				rightStart := task.start - leftCount
				if rightStart < 0 {
					rightStart = 0
				}
				rightEnd := end - leftCount
				if rightEnd > rightCount {
					rightEnd = rightCount
				}
				if rightEnd > rightStart {
					stack = append(stack, sequenceTask{id: node.Children[1], start: rightStart, count: rightEnd - rightStart})
				}
			}
			if leftEnd > task.start {
				stack = append(stack, sequenceTask{id: node.Children[0], start: task.start, count: leftEnd - task.start})
			}
		default:
			return nil, fmt.Errorf("%w: %s is not a sequence", ErrInvalid, task.id)
		}
	}
	if len(values) != count {
		return nil, fmt.Errorf("%w: sequence %s yielded %d items, expected %d", ErrInvalid, id, len(values), count)
	}
	return values, nil
}

func sequenceNodeCount(node Node) (int, error) {
	if !isSequenceKind(node.Kind) {
		return 0, fmt.Errorf("%w: %s is not a sequence", ErrInvalid, node.Kind)
	}
	return sequenceCount(node)
}
