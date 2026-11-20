package codexshim

import (
	"encoding/base64"
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

func localPath(raw string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "file" || (u.Host != "" && u.Host != "localhost") || !filepath.IsAbs(u.Path) || u.RawQuery != "" || u.Fragment != "" {
		return "", errors.New("expected absolute file URI")
	}
	return u.Path, nil
}

func filesystem(method string, p params) (any, error) {
	path, err := localPath(p.Path)
	if err != nil {
		return nil, err
	}
	// No-follow has different semantics for every operation and must never be silently ignored.
	if p.FollowSymlinks != nil && !*p.FollowSymlinks {
		return filesystemNoFollow(method, path, p)
	}
	switch method {
	case "fs/readFile":
		b, err := os.ReadFile(path)
		return map[string]any{"dataBase64": base64.StdEncoding.EncodeToString(b)}, err
	case "fs/writeFile":
		b, err := base64.StdEncoding.DecodeString(p.DataBase64)
		if err != nil {
			return nil, err
		}
		return map[string]any{}, os.WriteFile(path, b, 0666)
	case "fs/createDirectory":
		if p.Recursive == nil || *p.Recursive {
			err = os.MkdirAll(path, 0777)
		} else {
			err = os.Mkdir(path, 0777)
		}
		return map[string]any{}, err
	case "fs/canonicalize":
		v, err := filepath.EvalSymlinks(path)
		if err != nil {
			return nil, err
		}
		return map[string]any{"path": (&url.URL{Scheme: "file", Path: v}).String()}, nil
	case "fs/readDirectory":
		entries, err := os.ReadDir(path)
		if err != nil {
			return nil, err
		}
		out := make([]map[string]any, 0, len(entries))
		for _, e := range entries {
			info, err := e.Info()
			if err != nil {
				return nil, err
			}
			out = append(out, map[string]any{"fileName": e.Name(), "isDirectory": info.IsDir(), "isFile": info.Mode().IsRegular()})
		}
		return map[string]any{"entries": out}, nil
	case "fs/remove":
		if p.Recursive != nil && *p.Recursive {
			err = os.RemoveAll(path)
		} else {
			err = os.Remove(path)
		}
		if p.Force != nil && *p.Force && errors.Is(err, os.ErrNotExist) {
			err = nil
		}
		return map[string]any{}, err
	case "fs/walk":
		return walkFiles(path, p)

	case "fs/getMetadata":
		info, err := os.Stat(path)
		if err != nil {
			return nil, err
		}
		modified := info.ModTime().UnixMilli()
		created := int64(0)
		var stat unix.Statx_t
		if unix.Statx(unix.AT_FDCWD, path, 0, unix.STATX_BTIME, &stat) == nil && stat.Mask&unix.STATX_BTIME != 0 {
			created = stat.Btime.Sec*1000 + int64(stat.Btime.Nsec)/1e6
		}
		return map[string]any{"isDirectory": info.IsDir(), "isFile": info.Mode().IsRegular(), "isSymlink": info.Mode()&os.ModeSymlink != 0, "size": info.Size(), "createdAtMs": created, "modifiedAtMs": modified}, nil
	}
	return nil, errors.New("unsupported filesystem method")
}

func walkFiles(root string, p params) (any, error) {
	o := p.Options
	if o.MaxDepth < 0 || o.MaxDirectories <= 0 || o.MaxEntries <= 0 {
		return nil, errors.New("invalid walk bounds")
	}
	entries := []map[string]any{}
	failures := []map[string]any{}
	truncated := false
	dirs, count := 0, 0
	active := map[string]bool{}
	var walk func(string, int)
	walk = func(path string, depth int) {
		if truncated || depth > o.MaxDepth {
			return
		}
		info, err := os.Lstat(path)
		if err != nil {
			failures = append(failures, map[string]any{"path": uri(path), "message": err.Error()})
			return
		}
		if info.Mode()&os.ModeSymlink != 0 {
			if !o.FollowDirectorySymlinks {
				return
			}
			info, err = os.Stat(path)
			if err != nil {
				failures = append(failures, map[string]any{"path": uri(path), "message": err.Error()})
				return
			}
		}
		if info.IsDir() {
			dirs++
			if dirs > o.MaxDirectories {
				truncated = true
				return
			}
			if depth > 0 {
				entries = append(entries, map[string]any{"path": uri(path), "kind": "directory"})
				if o.PruneHiddenDirectories && strings.HasPrefix(info.Name(), ".") {
					return
				}
			}
			canonical, err := filepath.EvalSymlinks(path)
			if err != nil {
				failures = append(failures, map[string]any{"path": uri(path), "message": err.Error()})
				return
			}
			if active[canonical] {
				failures = append(failures, map[string]any{"path": uri(path), "message": "symlink cycle"})
				return
			}
			active[canonical] = true
			defer delete(active, canonical)
			children, err := os.ReadDir(path)
			if err != nil {
				failures = append(failures, map[string]any{"path": uri(path), "message": err.Error()})
				return
			}
			for _, child := range children {
				count++
				if count > o.MaxEntries {
					truncated = true
					return
				}
				walk(filepath.Join(path, child.Name()), depth+1)
			}
			return
		}
		if depth > 0 && info.Mode().IsRegular() {
			entries = append(entries, map[string]any{"path": uri(path), "kind": "file"})
		}
	}
	walk(root, 0)
	return map[string]any{"entries": entries, "errors": failures, "truncated": truncated}, nil
}
