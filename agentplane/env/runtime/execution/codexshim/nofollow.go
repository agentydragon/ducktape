package codexshim

import (
	"encoding/base64"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

// openat2 resolves every component without following links; an unsupported kernel
// returns an error, preserving the caller's requested restriction.
func filesystemNoFollow(method, path string, p params) (any, error) {
	if method == "fs/createDirectory" {
		return createNoFollow(path, p)
	}
	if method == "fs/remove" {
		return removeNoFollow(path, p)
	}
	var writeData []byte
	if method == "fs/writeFile" {
		var err error
		writeData, err = base64.StdEncoding.DecodeString(p.DataBase64)
		if err != nil {
			return nil, err
		}
	}
	var flags uint64
	switch method {
	case "fs/readFile":
		flags = unix.O_RDONLY | unix.O_NONBLOCK | unix.O_CLOEXEC
	case "fs/writeFile":
		flags = unix.O_WRONLY | unix.O_CREAT | unix.O_NONBLOCK | unix.O_CLOEXEC
	case "fs/getMetadata":
		flags = unix.O_PATH | unix.O_NOFOLLOW | unix.O_CLOEXEC
	default:
		return nil, errors.New("no-follow option unsupported for this filesystem operation")
	}
	fd, err := unix.Openat2(unix.AT_FDCWD, path, &unix.OpenHow{Flags: flags, Mode: func() uint64 {
		if method == "fs/writeFile" {
			return 0666
		}
		return 0
	}(), Resolve: unix.RESOLVE_NO_SYMLINKS})
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	switch method {
	case "fs/getMetadata":
		var stat unix.Statx_t
		created := int64(0)
		if unix.Statx(fd, "", unix.AT_EMPTY_PATH|unix.AT_SYMLINK_NOFOLLOW, unix.STATX_BTIME, &stat) == nil && stat.Mask&unix.STATX_BTIME != 0 {
			created = stat.Btime.Sec*1000 + int64(stat.Btime.Nsec)/1e6
		}
		return map[string]any{"isDirectory": info.IsDir(), "isFile": info.Mode().IsRegular(), "isSymlink": info.Mode()&os.ModeSymlink != 0, "size": info.Size(), "createdAtMs": created, "modifiedAtMs": info.ModTime().UnixMilli()}, nil
	case "fs/readFile":
		if !info.Mode().IsRegular() {
			return nil, errors.New("not a regular file")
		}
		data, err := io.ReadAll(file)
		return map[string]any{"dataBase64": base64.StdEncoding.EncodeToString(data)}, err
	case "fs/writeFile":
		if !info.Mode().IsRegular() {
			return nil, errors.New("not a regular file")
		}
		if err = file.Truncate(0); err != nil {
			return nil, err
		}
		_, err = file.Write(writeData)
		return map[string]any{}, err
	}
	return nil, errors.New("unsupported no-follow operation")
}

func noFollowParent(path string) (int, string, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || path == "/" {
		return -1, "", errors.New("expected normalized non-root path")
	}
	parent, leaf := filepath.Dir(path), filepath.Base(path)
	fd, err := unix.Openat2(unix.AT_FDCWD, parent, &unix.OpenHow{Flags: unix.O_PATH | unix.O_DIRECTORY | unix.O_CLOEXEC, Resolve: unix.RESOLVE_NO_SYMLINKS})
	return fd, leaf, err
}

func createNoFollow(path string, p params) (any, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return nil, errors.New("expected normalized absolute path")
	}
	if p.Recursive != nil && !*p.Recursive {
		fd, leaf, err := noFollowParent(path)
		if err != nil {
			return nil, err
		}
		defer unix.Close(fd)
		return map[string]any{}, unix.Mkdirat(fd, leaf, 0777)
	}
	fd, err := unix.Open("/", unix.O_PATH|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	defer func() { _ = unix.Close(fd) }()
	for _, part := range strings.Split(strings.TrimPrefix(path, "/"), "/") {
		if part == "" {
			continue
		}
		err = unix.Mkdirat(fd, part, 0777)
		if err != nil && !errors.Is(err, unix.EEXIST) {
			return nil, err
		}
		next, e := unix.Openat(fd, part, unix.O_PATH|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if e != nil {
			return nil, e
		}
		_ = unix.Close(fd)
		fd = next
	}
	return map[string]any{}, nil
}

func removeNoFollow(path string, p params) (any, error) {
	fd, leaf, err := noFollowParent(path)
	if err != nil {
		if p.Force != nil && *p.Force && errors.Is(err, unix.ENOENT) {
			return map[string]any{}, nil
		}
		return nil, err
	}
	defer unix.Close(fd)
	err = removeAt(fd, leaf, p.Recursive != nil && *p.Recursive)
	if p.Force != nil && *p.Force && errors.Is(err, unix.ENOENT) {
		err = nil
	}
	return map[string]any{}, err
}

func removeAt(parent int, name string, recursive bool) error {
	var st unix.Stat_t
	if err := unix.Fstatat(parent, name, &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		return err
	}
	if st.Mode&unix.S_IFMT != unix.S_IFDIR {
		return unix.Unlinkat(parent, name, 0)
	}
	if !recursive {
		return unix.Unlinkat(parent, name, unix.AT_REMOVEDIR)
	}
	fd, err := unix.Openat(parent, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return err
	}
	f := os.NewFile(uintptr(fd), name)
	names, err := f.Readdirnames(-1)
	if err != nil {
		_ = f.Close()
		return err
	}
	for _, child := range names {
		if err = removeAt(fd, child, true); err != nil {
			_ = f.Close()
			return err
		}
	}
	if err = f.Close(); err != nil {
		return err
	}
	return unix.Unlinkat(parent, name, unix.AT_REMOVEDIR)
}
