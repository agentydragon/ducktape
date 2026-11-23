package codexshim

import (
	"cmp"
	"container/heap"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"golang.org/x/sys/unix"
)

const (
	directoryReadBatchSize         = 128
	defaultMaxReadDirectoryEntries = 50_000
	defaultMaxReadFileBytes        = 512 << 20
	defaultMaxWalkDepth            = 64
	defaultMaxWalkDirectories      = 10_000
	defaultMaxWalkEntries          = 50_000
	defaultMaxWalkResponseBytes    = 4 << 20
	minWalkResponseBytes           = len(`{"entries":[],"errors":[],"truncated":false}`)
)

func filesystemRPCError(err error) error {
	if err == nil {
		return nil
	}
	var rpc rpcError
	if errors.As(err, &rpc) {
		return rpc
	}
	code := -32603
	switch {
	case errors.Is(err, os.ErrNotExist):
		code = -32004
	case errors.Is(err, os.ErrPermission), errors.Is(err, os.ErrInvalid), errors.Is(err, unix.EINVAL), errors.Is(err, native.ErrInvalidRequest):
		code = -32600
	}
	var errno unix.Errno
	if errors.As(err, &errno) {
		message := errno.Error()
		return rpcError{code, fmt.Sprintf("%s%s (os error %d)", strings.ToUpper(message[:1]), message[1:], errno)}
	}
	return rpcError{code, err.Error()}
}

func localPath(raw string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "file" || !filepath.IsAbs(u.Path) || u.User != nil || u.Port() != "" || u.ForceQuery || u.RawQuery != "" || u.Fragment != "" || strings.ContainsRune(u.Path, 0) {
		return "", rpcError{-32602, "expected absolute file URI"}
	}
	if u.Host != "" && !strings.EqualFold(u.Host, "localhost") {
		return "", rpcError{-32600, fmt.Sprintf("'%s' is invalid on 'linux'", raw)}
	}
	return u.Path, nil
}

func filesystem(method string, p params) (any, error) {
	path, err := localPath(p.Path)
	if err != nil {
		return nil, err
	}
	if p.FollowSymlinks != nil && !*p.FollowSymlinks {
		switch method {
		case "fs/readFile", "fs/writeFile", "fs/createDirectory", "fs/getMetadata", "fs/remove":
			return filesystemNoFollow(method, path, p)
		}
	}
	switch method {
	case "fs/readFile":
		file, err := os.OpenFile(path, os.O_RDONLY|unix.O_NONBLOCK, 0)
		if err != nil {
			return nil, err
		}
		defer file.Close()
		info, err := file.Stat()
		if err != nil {
			return nil, err
		}
		if !info.Mode().IsRegular() {
			return nil, rpcError{-32600, fmt.Sprintf("path `%s` is not a file", path)}
		}
		return readFileResult(file, info.Size(), p.maxReadFileBytes)
	case "fs/writeFile":
		b, err := decodeFileContents(p.DataBase64)
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
		return readDirectory(path, p.maxReadDirectoryEntries)
	case "fs/remove":
		info, err := os.Lstat(path)
		if err != nil {
			if (p.Force == nil || *p.Force) && errors.Is(err, os.ErrNotExist) {
				return map[string]any{}, nil
			}
			return nil, err
		}
		if !info.IsDir() {
			return map[string]any{}, unix.Unlink(path)
		}
		if p.Recursive == nil || *p.Recursive {
			return map[string]any{}, os.RemoveAll(path)
		}
		return map[string]any{}, unix.Rmdir(path)
	case "fs/walk":
		return walkFiles(path, p)

	case "fs/getMetadata":
		info, err := os.Lstat(path)
		if err != nil {
			return nil, err
		}
		isSymlink := info.Mode()&os.ModeSymlink != 0
		if isSymlink {
			info, err = os.Stat(path)
			if err != nil {
				return nil, err
			}
		}
		return metadataResult(info, isSymlink, unix.AT_FDCWD, path, 0), nil
	}
	return nil, errors.New("unsupported filesystem method")
}

func decodeFileContents(data string) ([]byte, error) {
	if strings.ContainsAny(data, "\r\n") {
		return nil, rpcError{-32600, "fs/writeFile requires valid base64 dataBase64"}
	}
	contents, err := base64.StdEncoding.Strict().DecodeString(data)
	if err != nil {
		return nil, rpcError{-32600, fmt.Sprintf("fs/writeFile requires valid base64 dataBase64: %v", err)}
	}
	return contents, nil
}

func readFileResult(file *os.File, size, limit int64) (any, error) {
	limit = cmp.Or(limit, defaultMaxReadFileBytes)
	tooLarge := rpcError{-32600, fmt.Sprintf("file is too large to read: limit is %d bytes", limit)}
	if size > limit {
		return nil, tooLarge
	}
	data, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, tooLarge
	}
	return map[string]any{"dataBase64": base64.StdEncoding.EncodeToString(data)}, nil
}

func readDirectory(path string, limit int) (any, error) {
	limit = cmp.Or(limit, defaultMaxReadDirectoryEntries)
	dir, err := os.OpenFile(path, os.O_RDONLY|unix.O_DIRECTORY, 0)
	if err != nil {
		return nil, err
	}
	defer dir.Close()
	type entry struct {
		FileName    string `json:"fileName"`
		IsDirectory bool   `json:"isDirectory"`
		IsFile      bool   `json:"isFile"`
	}
	entries := []entry{}
	for {
		batch, err := dir.ReadDir(directoryReadBatchSize)
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		for _, child := range batch {
			kind := child.Type()
			e := entry{child.Name(), kind.IsDir(), kind.IsRegular()}
			if kind&os.ModeSymlink != 0 {
				var stat unix.Stat_t
				if err := unix.Fstatat(int(dir.Fd()), e.FileName, &stat, 0); err != nil {
					continue
				}
				e.IsDirectory = stat.Mode&unix.S_IFMT == unix.S_IFDIR
				e.IsFile = stat.Mode&unix.S_IFMT == unix.S_IFREG
			}
			if len(entries) == limit {
				return nil, rpcError{-32603, fmt.Sprintf("fs/readDirectory returned more than %d entries", limit)}
			}
			entries = append(entries, e)
		}
	}
	slices.SortFunc(entries, func(a, b entry) int { return cmp.Compare(a.FileName, b.FileName) })
	return map[string]any{"entries": entries}, nil
}

func metadataResult(info os.FileInfo, isSymlink bool, fd int, path string, flags int) map[string]any {
	created := int64(0)
	var stat unix.Statx_t
	if unix.Statx(fd, path, flags, unix.STATX_BTIME, &stat) == nil && stat.Mask&unix.STATX_BTIME != 0 {
		created = stat.Btime.Sec*1000 + int64(stat.Btime.Nsec)/1e6
	}
	return map[string]any{"isDirectory": info.IsDir(), "isFile": info.Mode().IsRegular(), "isSymlink": isSymlink, "size": info.Size(), "createdAtMs": created, "modifiedAtMs": info.ModTime().UnixMilli()}
}

func walkFiles(root string, p params) (any, error) {
	o := p.Options
	if o.MaxDepth < 0 || o.MaxDirectories <= 0 || o.MaxEntries <= 0 {
		return nil, rpcError{-32600, "filesystem walk limits must be greater than zero"}
	}
	depthLimit := cmp.Or(p.maxWalkDepth, defaultMaxWalkDepth)
	directoryLimit := cmp.Or(p.maxWalkDirectories, defaultMaxWalkDirectories)
	entryLimit := cmp.Or(p.maxWalkEntries, defaultMaxWalkEntries)
	responseLimit := cmp.Or(p.maxWalkResponseBytes, defaultMaxWalkResponseBytes)
	if o.MaxDepth > depthLimit || o.MaxDirectories > directoryLimit || o.MaxEntries > entryLimit {
		return nil, rpcError{-32600, fmt.Sprintf("filesystem walk limits exceed maximums: depth=%d, directories=%d, entries=%d", depthLimit, directoryLimit, entryLimit)}
	}
	entries := []map[string]string{}
	failures := []map[string]string{}
	truncated := false
	remaining := responseLimit - minWalkResponseBytes
	appendItem := func(items *[]map[string]string, item map[string]string) {
		encoded, _ := json.Marshal(item)
		// Reserve a comma for every item, including the first in each array.
		remaining -= len(encoded) + 1
		if remaining < 0 {
			truncated = true
			return
		}
		*items = append(*items, item)
	}
	var rootErr error
	recordError := func(path string, err error) {
		if path == root {
			rootErr = err
			return
		}
		appendItem(&failures, map[string]string{"path": uri(path), "message": err.Error()})
	}
	dirs, count := 0, 0
	active := map[string]bool{}
	var walk func(string, int)
	walk = func(path string, depth int) {
		if truncated || depth > o.MaxDepth {
			return
		}
		info, err := os.Lstat(path)
		if err != nil {
			recordError(path, err)
			return
		}
		if info.Mode()&os.ModeSymlink != 0 {
			if !o.FollowDirectorySymlinks {
				return
			}
			info, err = os.Stat(path)
			if err != nil {
				recordError(path, err)
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
				appendItem(&entries, map[string]string{"path": uri(path), "kind": "directory"})
				if truncated || (o.PruneHiddenDirectories && strings.HasPrefix(info.Name(), ".")) {
					return
				}
			}
			canonical, err := filepath.EvalSymlinks(path)
			if err != nil {
				recordError(path, err)
				return
			}
			if active[canonical] {
				recordError(path, errors.New("symlink cycle"))
				return
			}
			active[canonical] = true
			defer delete(active, canonical)
			children, extra, err := walkDirectoryNames(path, o.MaxEntries-count)
			if err != nil {
				appendItem(&failures, map[string]string{"path": uri(path), "message": err.Error()})
				return
			}
			for _, child := range children {
				if count == o.MaxEntries {
					truncated = true
					return
				}
				count++
				walk(filepath.Join(path, child), depth+1)
				if truncated {
					return
				}
			}
			truncated = extra
			return
		}
		if depth > 0 && info.Mode().IsRegular() {
			appendItem(&entries, map[string]string{"path": uri(path), "kind": "file"})
		}
	}
	walk(root, 0)
	if rootErr != nil {
		return nil, rootErr
	}
	return map[string]any{"entries": entries, "errors": failures, "truncated": truncated}, nil
}

// Keep only the lexical prefix the walk can consume, even when the directory
// contains many more names than the entry budget.
func walkDirectoryNames(path string, limit int) ([]string, bool, error) {
	dir, err := os.OpenFile(path, os.O_RDONLY|unix.O_DIRECTORY, 0)
	if err != nil {
		return nil, false, err
	}
	defer dir.Close()
	if limit == 0 {
		names, err := dir.Readdirnames(1)
		if err == io.EOF {
			err = nil
		}
		return nil, len(names) > 0, err
	}
	var prefix directoryNameHeap
	extra := false
	for {
		batch, err := dir.Readdirnames(directoryReadBatchSize)
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, false, err
		}
		for _, name := range batch {
			if len(prefix) < limit {
				heap.Push(&prefix, name)
				continue
			}
			extra = true
			if name < prefix[0] {
				prefix[0] = name
				heap.Fix(&prefix, 0)
			}
		}
	}
	slices.Sort(prefix)
	return prefix, extra, nil
}

type directoryNameHeap []string

func (h directoryNameHeap) Len() int           { return len(h) }
func (h directoryNameHeap) Less(i, j int) bool { return h[i] > h[j] }
func (h directoryNameHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }
func (h *directoryNameHeap) Push(name any)     { *h = append(*h, name.(string)) }
func (h *directoryNameHeap) Pop() any {
	i := len(*h) - 1
	name := (*h)[i]
	(*h)[i] = ""
	*h = (*h)[:i]
	return name
}
