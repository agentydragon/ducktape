package codexshim

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"
)

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
func discover(p params) (any, error) {
	if len(p.Roots) > 128 {
		return nil, errors.New("capability root discovery accepts at most 128 roots")
	}
	result := make([]rootResult, 0, len(p.Roots))
	for _, r := range p.Roots {
		if nonnull(r.Sandbox) {
			return nil, errors.New("sandboxed discovery unsupported")
		}
		out := rootResult{ID: r.ID, Path: r.Path, Skills: []skillFiles{}, Namespace: []*textFile{}, Warnings: []string{}}
		root, err := localPath(r.Path)
		if err != nil {
			message := err.Error()
			out.Error = &message
			result = append(result, out)
			continue
		}
		info, err := os.Stat(root)
		if err != nil || !info.IsDir() {
			message := fmt.Sprintf("failed to inspect capability root %s: %v", r.Path, err)
			out.Error = &message
			result = append(result, out)
			continue
		}
		var budget int
		read := func(path string) *textFile {
			info, err := os.Stat(path)
			if errors.Is(err, os.ErrNotExist) {
				return nil
			}
			if err != nil {
				out.Warnings = append(out.Warnings, fmt.Sprintf("failed to inspect capability file %s: %v", uri(path), err))
				return nil
			}
			if !info.Mode().IsRegular() {
				return nil
			}
			if info.Size() > 1<<20 {
				out.Warnings = append(out.Warnings, fmt.Sprintf("capability file %s exceeds the 1048576-byte limit", uri(path)))
				return nil
			}
			if budget+int(info.Size()) > 16<<20 {
				out.Warnings = append(out.Warnings, "capability root bundle exceeds the 16777216-byte limit")
				return nil
			}
			b, err := os.ReadFile(path)
			if err != nil || len(b) > 1<<20 || !utf8.Valid(b) {
				out.Warnings = append(out.Warnings, fmt.Sprintf("failed to read capability file %s", uri(path)))
				return nil
			}
			budget += len(b)
			return &textFile{Path: uri(path), Contents: string(b)}
		}
		firstManifest := func(dir string) *textFile {
			for _, relative := range pluginPaths {
				if v := read(filepath.Join(dir, relative)); v != nil {
					return v
				}
			}
			return nil
		}

		var skills, manifests []string
		dirs, entries := 0, 0
		truncated := false
		active := map[string]bool{}
		var walk func(string, int)
		walk = func(path string, depth int) {
			if depth > 6 || truncated {
				return
			}
			info, e := os.Stat(path)
			if e != nil {
				out.Warnings = append(out.Warnings, fmt.Sprintf("failed to scan capability path %s: %v", uri(path), e))
				return
			}
			if info.IsDir() {
				dirs++
				if dirs > 2000 {
					truncated = true
					return
				}
				canonical, e := filepath.EvalSymlinks(path)
				if e != nil {
					out.Warnings = append(out.Warnings, fmt.Sprintf("failed to scan capability path %s: %v", uri(path), e))
					return
				}
				if active[canonical] {
					out.Warnings = append(out.Warnings, fmt.Sprintf("failed to scan capability path %s: symlink cycle", uri(path)))
					return
				}
				active[canonical] = true
				defer delete(active, canonical)
				children, e := os.ReadDir(path)
				if e != nil {
					out.Warnings = append(out.Warnings, fmt.Sprintf("failed to scan capability path %s: %v", uri(path), e))
					return
				}
				for _, child := range children {
					entries++
					if entries > 20000 {
						truncated = true
						return
					}
					walk(filepath.Join(path, child.Name()), depth+1)
				}
				return
			}
			if info.Mode().IsRegular() {
				if filepath.Base(path) == "SKILL.md" {
					skills = append(skills, path)
				}
				for _, mp := range pluginPaths {
					if strings.HasSuffix(filepath.ToSlash(path), "/"+mp) {
						manifests = append(manifests, path)
						break
					}
				}
			}
		}
		walk(root, 0)

		if truncated {
			out.Warnings = append(out.Warnings, fmt.Sprintf("capability scan reached its traversal limit (root: %s)", r.Path))
		}
		sort.Strings(skills)
		sort.Slice(manifests, func(i, j int) bool {
			ri, rj := filepath.Dir(filepath.Dir(manifests[i])), filepath.Dir(filepath.Dir(manifests[j]))
			if ri != rj {
				return ri < rj
			}
			priority := func(p string) int {
				for i, x := range pluginPaths {
					if strings.HasSuffix(filepath.ToSlash(p), "/"+x) {
						return i
					}
				}
				return len(pluginPaths)
			}
			return priority(manifests[i]) < priority(manifests[j])
		})
		rootManifest := firstManifest(root)
		inherited := rootManifest
		if inherited == nil {
			for ancestor := filepath.Dir(root); ; ancestor = filepath.Dir(ancestor) {
				inherited = firstManifest(ancestor)
				if inherited != nil || ancestor == filepath.Dir(ancestor) {
					break
				}
			}
		}
		seen := map[string]bool{}
		if inherited != nil {
			path, _ := localPath(inherited.Path)
			seen[filepath.Dir(filepath.Dir(path))] = true
			out.Namespace = append(out.Namespace, inherited)
		}
		for _, path := range manifests {
			dir := filepath.Dir(filepath.Dir(path))
			if seen[dir] {
				continue
			}
			seen[dir] = true
			if v := read(path); v != nil {
				out.Namespace = append(out.Namespace, v)
			}
		}
		if rootManifest != nil {
			var data map[string]any
			_ = json.Unmarshal([]byte(rootManifest.Contents), &data)
			configPath := func(key, def string) *textFile {
				v, ok := data[key]
				if obj, yes := v.(map[string]any); yes && key == "mcpServers" && obj != nil {
					return nil
				}
				if value, yes := v.(string); yes {
					if !strings.HasPrefix(value, "./") || invalidRelative(value) {
						out.Warnings = append(out.Warnings, fmt.Sprintf("ignoring %s in %s: path must remain below the capability root", key, rootManifest.Path))
						return read(filepath.Join(root, def))
					}
					return read(filepath.Join(root, value))
				}
				if ok && v != nil {
				}
				return read(filepath.Join(root, def))
			}
			out.Plugin = &pluginFiles{Manifest: rootManifest, MCP: configPath("mcpServers", ".mcp.json"), Apps: configPath("apps", ".app.json")}
		}
		for _, path := range skills {
			v := read(path)
			if v != nil {
				out.Skills = append(out.Skills, skillFiles{Instructions: v, Metadata: read(filepath.Join(filepath.Dir(path), "agents/openai.yaml"))})
			}
		}
		result = append(result, out)
	}
	return map[string]any{"roots": result}, nil
}

func invalidRelative(value string) bool {
	if value == "./" {
		return true
	}
	for _, part := range strings.FieldsFunc(value, func(r rune) bool { return r == '/' || r == '\\' }) {
		if part == ".." {
			return true
		}
	}
	return false
}
