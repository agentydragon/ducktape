package codexshim

import (
	"bytes"
	"cmp"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"

	"golang.org/x/sys/unix"
)

const maxCapabilityWalkBytes = 4 << 20

var pluginPaths = []string{".codex-plugin/plugin.json", ".claude-plugin/plugin.json", ".cursor-plugin/plugin.json"}

type textFile struct {
	Path     string `json:"path"`
	Contents string `json:"contents"`
}
type pluginFiles struct {
	Manifest *textFile `json:"manifest"`
	MCP      *textFile `json:"mcpConfig"`
	Apps     *textFile `json:"appsConfig"`
}
type skillFiles struct {
	Instructions *textFile `json:"instructions"`
	Metadata     *textFile `json:"metadata"`
}
type rootResult struct {
	ID        string       `json:"id"`
	Path      string       `json:"path"`
	Plugin    *pluginFiles `json:"plugin"`
	Skills    []skillFiles `json:"skills"`
	Namespace []*textFile  `json:"namespaceManifests"`
	Warnings  []string     `json:"warnings"`
	Error     *string      `json:"error"`
}

func uri(path string) string { return (&url.URL{Scheme: "file", Path: path}).String() }

type capabilityLimits struct {
	depth, directories, entries int
	fileBytes, bundleBytes      int
}

func discover(p params) (any, error) {
	defaults := DefaultLimits()
	maxRoots := cmp.Or(p.maxDiscoveryRoots, defaults.MaxDiscoveryRoots)
	if len(p.Roots) > maxRoots {
		return nil, rpcError{-32600, fmt.Sprintf("capability root discovery accepts at most %d roots", maxRoots)}
	}
	limits := capabilityLimits{
		depth:       cmp.Or(p.maxDiscoveryDepth, defaults.MaxDiscoveryDepth),
		directories: cmp.Or(p.maxDiscoveryDirectories, defaults.MaxDiscoveryDirectories),
		entries:     cmp.Or(p.maxDiscoveryEntries, defaults.MaxDiscoveryEntries),
		fileBytes:   cmp.Or(p.maxDiscoveryFileBytes, defaults.MaxDiscoveryFileBytes),
		bundleBytes: cmp.Or(p.maxDiscoveryBundleBytes, defaults.MaxDiscoveryBundleBytes),
	}
	result := make([]rootResult, 0, len(p.Roots))
	for _, r := range p.Roots {
		root, err := localPath(r.Path)
		if err != nil {
			return nil, err
		}
		out := rootResult{ID: r.ID, Path: r.Path, Skills: []skillFiles{}, Namespace: []*textFile{}, Warnings: []string{}}
		if !capabilityReadsUnrestricted(r.Sandbox) {
			message := "filesystem sandbox is unavailable on this executor"
			out.Error = &message
		} else {
			out.discover(filepath.Clean(root), limits)
		}
		result = append(result, out)
	}
	return map[string]any{"roots": result}, nil
}

func (out *rootResult) discover(root string, limits capabilityLimits) {
	info, err := os.Stat(root)
	if err != nil {
		message := fmt.Sprintf("failed to inspect capability root %s: %v", out.Path, err)
		out.Error = &message
		return
	}
	if !info.IsDir() {
		message := fmt.Sprintf("capability root %s is not a directory", out.Path)
		out.Error = &message
		return
	}

	skills, manifests, err := out.walk(root, limits)
	if err != nil {
		message := fmt.Sprintf("failed to scan capability root %s: %v", out.Path, err)
		out.Error = &message
		return
	}
	sort.Slice(skills, func(i, j int) bool {
		return capabilityURI(skills[i]) < capabilityURI(skills[j])
	})
	sort.Slice(manifests, func(i, j int) bool {
		left, right := capabilityURI(pluginRoot(manifests[i])), capabilityURI(pluginRoot(manifests[j]))
		if left != right {
			return left < right
		}
		return pluginPriority(manifests[i]) < pluginPriority(manifests[j])
	})

	reader := capabilityReader{fileLimit: limits.fileBytes, bundleLimit: limits.bundleBytes, warnings: &out.Warnings}
	rootManifest := reader.firstManifest(root)
	inherited := rootManifest
	for dir := root; inherited == nil && dir != filepath.Dir(dir); {
		dir = filepath.Dir(dir)
		inherited = reader.firstManifest(dir)
	}
	seen := map[string]bool{}
	if inherited != nil {
		path, _ := localPath(inherited.Path)
		seen[pluginRoot(path)] = true
		out.Namespace = append(out.Namespace, inherited)
	}
	for _, path := range manifests {
		dir := pluginRoot(path)
		if seen[dir] {
			continue
		}
		seen[dir] = true
		if manifest := reader.read(path); manifest != nil {
			out.Namespace = append(out.Namespace, manifest)
		}
	}
	if rootManifest != nil {
		var declarations map[string]json.RawMessage
		if json.Unmarshal([]byte(rootManifest.Contents), &declarations) != nil {
			declarations = nil
		}
		mcp := bytes.TrimSpace(declarations["mcpServers"])
		var mcpConfig *textFile
		if len(mcp) == 0 || mcp[0] != '{' {
			mcpConfig = reader.config(root, rootManifest.Path, "mcpServers", mcp, ".mcp.json")
		}
		out.Plugin = &pluginFiles{
			Manifest: rootManifest,
			MCP:      mcpConfig,
			Apps:     reader.config(root, rootManifest.Path, "apps", declarations["apps"], ".app.json"),
		}
	}
	for _, path := range skills {
		if instructions := reader.read(path); instructions != nil {
			out.Skills = append(out.Skills, skillFiles{
				Instructions: instructions,
				Metadata:     reader.read(filepath.Join(filepath.Dir(path), "agents/openai.yaml")),
			})
		}
	}
}

// The exec-server follows directory aliases once in breadth-first order. A directory at
// the depth limit still contributes its files.
func (out *rootResult) walk(root string, limits capabilityLimits) (skills, manifests []string, err error) {
	type directory struct {
		path  string
		depth int
	}
	canonical, err := filepath.EvalSymlinks(root)
	if err != nil {
		return nil, nil, err
	}
	queue := []directory{{path: root}}
	visited := map[string]bool{canonical: true}
	entryCount, walkBytes := 0, 0
	truncated := false
	defer func() {
		if truncated {
			out.Warnings = append(out.Warnings, fmt.Sprintf("capability scan reached its traversal limit (root: %s)", out.Path))
		}
	}()
	reserve := func(path, message string) bool {
		walkBytes += len(path) + len(message) + 64 // The Codex fs/walk per-item allowance.
		if walkBytes > maxCapabilityWalkBytes {
			truncated = true
			return false
		}
		return true
	}
	warn := func(path string, err error) bool {
		path = capabilityURI(path)
		if !reserve(path, err.Error()) {
			return false
		}
		out.Warnings = append(out.Warnings, fmt.Sprintf("failed to scan capability path %s: %v", path, err))
		return true
	}
	for index := 0; index < len(queue); index++ {
		dir := queue[index]
		children, err := os.ReadDir(dir.path)
		if err != nil {
			if !warn(dir.path, err) {
				break
			}
			continue
		}
		for _, child := range children {
			path := filepath.Join(dir.path, child.Name())
			var info os.FileInfo
			var err error
			isLink := child.Type()&os.ModeSymlink != 0
			if isLink {
				info, err = os.Stat(path)
				if err != nil {
					continue // Match Codex directory enumeration by omitting broken links.
				}
			}
			if entryCount == limits.entries {
				truncated = true
				return skills, manifests, nil
			}
			entryCount++
			if !isLink {
				info, err = child.Info()
			}
			if err != nil {
				if !warn(path, err) {
					return skills, manifests, nil
				}
				continue
			}
			if !info.IsDir() && (isLink || !info.Mode().IsRegular()) {
				continue
			}
			if !reserve(capabilityURI(path), "") {
				return skills, manifests, nil
			}
			if info.IsDir() {
				if dir.depth == limits.depth {
					continue
				}
				canonical, err := filepath.EvalSymlinks(path)
				if err != nil {
					if !warn(path, err) {
						return skills, manifests, nil
					}
					continue
				}
				if visited[canonical] {
					continue
				}
				visited[canonical] = true
				if len(queue) == limits.directories {
					truncated = true
					continue
				}
				queue = append(queue, directory{path, dir.depth + 1})
			} else if child.Name() == "SKILL.md" {
				skills = append(skills, path)
			} else if pluginPriority(path) < len(pluginPaths) {
				manifests = append(manifests, path)
			}
		}
	}
	return skills, manifests, nil
}

func pluginRoot(path string) string { return filepath.Dir(filepath.Dir(path)) }

func pluginPriority(path string) int {
	for priority, relative := range pluginPaths {
		if strings.HasSuffix(path, "/"+relative) {
			return priority
		}
	}
	return len(pluginPaths)
}

type capabilityReader struct {
	bytesRead   int
	fileLimit   int
	bundleLimit int
	warnings    *[]string
}

func (r *capabilityReader) warn(format string, args ...any) {
	*r.warnings = append(*r.warnings, fmt.Sprintf(format, args...))
}

func (r *capabilityReader) firstManifest(root string) *textFile {
	for _, relative := range pluginPaths {
		if manifest := r.read(filepath.Join(root, relative)); manifest != nil {
			return manifest
		}
	}
	return nil
}

func (r *capabilityReader) read(path string) *textFile {
	pathURI := capabilityURI(path)
	info, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		r.warn("failed to inspect capability file %s: %v", pathURI, err)
		return nil
	}
	if !info.Mode().IsRegular() {
		return nil
	}
	if info.Size() > int64(r.fileLimit) {
		r.warn("capability file %s exceeds the %d-byte limit", pathURI, r.fileLimit)
		return nil
	}
	remaining := r.bundleLimit - r.bytesRead
	if info.Size() > int64(remaining) {
		r.warn("capability root bundle exceeds the %d-byte limit", r.bundleLimit)
		return nil
	}
	// A replacement FIFO must not block discovery between the metadata check and the open.
	file, err := os.OpenFile(path, os.O_RDONLY|unix.O_NONBLOCK, 0)
	if err != nil {
		r.warn("failed to read capability file %s: %v", pathURI, err)
		return nil
	}
	defer file.Close()
	info, err = file.Stat()
	if err != nil {
		r.warn("failed to read capability file %s: %v", pathURI, err)
		return nil
	}
	if !info.Mode().IsRegular() {
		return nil
	}
	limit := min(r.fileLimit, remaining)
	readLimit := int64(limit)
	if readLimit < math.MaxInt64 {
		readLimit++
	}
	contents, err := io.ReadAll(io.LimitReader(file, readLimit))
	if err != nil {
		r.warn("failed to read capability file %s: %v", pathURI, err)
		return nil
	}
	if len(contents) > limit {
		r.warn("capability file %s exceeded its read limit", pathURI)
		return nil
	}
	if !utf8.Valid(contents) {
		r.warn("capability file %s is not UTF-8", pathURI)
		return nil
	}
	r.bytesRead += len(contents)
	return &textFile{Path: pathURI, Contents: string(contents)}
}

func (r *capabilityReader) config(root, manifest, field string, declaration json.RawMessage, fallback string) *textFile {
	var value *string
	if json.Unmarshal(declaration, &value) != nil || value == nil {
		return r.read(filepath.Join(root, fallback))
	}
	relative, startsRelative := strings.CutPrefix(*value, "./")
	if !startsRelative {
		r.warn("ignoring %s in %s: path must start with `./`", field, manifest)
		return r.read(filepath.Join(root, fallback))
	}
	path := filepath.Join(root, relative)
	if filepath.IsAbs(relative) {
		path = filepath.Clean(relative)
	}
	descendant, err := filepath.Rel(root, path)
	escapes := err != nil || descendant == ".." || strings.HasPrefix(descendant, "../")
	if relative == "" || strings.ContainsRune(relative, 0) || invalidRelative(relative) || escapes {
		r.warn("ignoring %s in %s: path must remain below the capability root", field, manifest)
		return r.read(filepath.Join(root, fallback))
	}
	return r.read(path)
}

func invalidRelative(value string) bool {
	for _, part := range strings.FieldsFunc(value, func(r rune) bool { return r == '/' || r == '\\' }) {
		if part == ".." {
			return true
		}
	}
	return false
}

// Match the Codex PathUri path-segment spelling, which the upstream service sorts
// before materializing skills and namespace manifests.
func capabilityURI(path string) string {
	const hex = "0123456789ABCDEF"
	var uri strings.Builder
	uri.WriteString("file://")
	for i := 0; i < len(path); i++ {
		char := path[i]
		if char <= ' ' || char >= 0x7f || strings.ContainsRune("\"#%<>?\\`{}", rune(char)) {
			uri.WriteByte('%')
			uri.WriteByte(hex[char>>4])
			uri.WriteByte(hex[char&15])
		} else {
			uri.WriteByte(char)
		}
	}
	return uri.String()
}

func capabilityReadsUnrestricted(sandbox json.RawMessage) bool {
	if !nonnull(sandbox) {
		return true
	}
	var context struct {
		Permissions json.RawMessage `json:"permissions"`
	}
	if json.Unmarshal(sandbox, &context) != nil {
		return false
	}
	var permissions struct {
		Type       string `json:"type"`
		Network    string `json:"network"`
		FileSystem *struct {
			Type             string `json:"type"`
			GlobScanMaxDepth *int   `json:"glob_scan_max_depth"`
			Entries          []struct {
				Access              string          `json:"access"`
				Path                json.RawMessage `json:"path"`
				MissingPathBehavior string          `json:"missing_path_behavior"`
			} `json:"entries"`
		} `json:"file_system"`
	}
	decoder := json.NewDecoder(bytes.NewReader(context.Permissions))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&permissions) != nil {
		return false
	}
	if permissions.Type == "disabled" {
		return permissions.FileSystem == nil && permissions.Network == ""
	}
	if permissions.Type != "managed" || permissions.FileSystem == nil {
		return false
	}
	if permissions.FileSystem.Type == "unrestricted" {
		return len(permissions.FileSystem.Entries) == 0 && permissions.FileSystem.GlobScanMaxDepth == nil
	}
	if permissions.FileSystem.Type != "restricted" {
		return false
	}
	// A root grant without any read denial needs no filesystem isolation, even
	// when the profile restricts writes. Unknown entry access modes fail closed.
	rootRead := false
	for _, entry := range permissions.FileSystem.Entries {
		if entry.Access != "read" && entry.Access != "write" {
			return false
		}
		var path struct {
			Type  string         `json:"type"`
			Value map[string]any `json:"value"`
		}
		decoder := json.NewDecoder(bytes.NewReader(entry.Path))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&path) == nil && path.Type == "special" && len(path.Value) == 1 && path.Value["kind"] == "root" {
			rootRead = true
		}
	}
	return rootRead
}
