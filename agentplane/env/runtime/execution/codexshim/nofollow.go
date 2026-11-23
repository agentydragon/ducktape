package codexshim

import (
	"errors"
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
		writeData, err = decodeFileContents(p.DataBase64)
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
		if info.Mode()&os.ModeSymlink != 0 {
			return nil, rpcError{-32600, "path contains a symbolic link"}
		}
		return metadataResult(info, false, fd, "", unix.AT_EMPTY_PATH|unix.AT_SYMLINK_NOFOLLOW), nil
	case "fs/readFile":
		if !info.Mode().IsRegular() {
			return nil, rpcError{-32600, "path is not a regular file"}
		}
		return readFileResult(file, info.Size(), p.maxReadFileBytes)
	case "fs/writeFile":
		if !info.Mode().IsRegular() {
			return nil, rpcError{-32600, "path is not a regular file"}
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
	if path == "/" {
		return -1, "", rpcError{-32600, "path must name an entry"}
	}
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return -1, "", rpcError{-32600, "no-follow filesystem operations require a normalized path"}
	}
	parent, leaf := filepath.Dir(path), filepath.Base(path)
	fd, err := unix.Openat2(unix.AT_FDCWD, parent, &unix.OpenHow{Flags: unix.O_PATH | unix.O_DIRECTORY | unix.O_CLOEXEC, Resolve: unix.RESOLVE_NO_SYMLINKS})
	return fd, leaf, err
}

func createNoFollow(path string, p params) (any, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return nil, rpcError{-32600, "no-follow filesystem operations require a normalized path"}
	}
	if p.Recursive != nil && !*p.Recursive {
		if path == "/" {
			return nil, rpcError{-32603, "directory already exists"}
		}
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
	if p.Recursive == nil || *p.Recursive {
		return nil, rpcError{-32603, "recursive no-follow removal is unsupported"}
	}
	force := p.Force == nil || *p.Force
	fd, leaf, err := noFollowParent(path)
	if err != nil {
		if force && errors.Is(err, unix.ENOENT) {
			return map[string]any{}, nil
		}
		return nil, err
	}
	defer unix.Close(fd)
	var st unix.Stat_t
	if err := unix.Fstatat(fd, leaf, &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		if force && errors.Is(err, unix.ENOENT) {
			return map[string]any{}, nil
		}
		return nil, err
	}
	var flags int
	switch st.Mode & unix.S_IFMT {
	case unix.S_IFLNK:
		return nil, rpcError{-32600, "path contains a symbolic link"}
	case unix.S_IFDIR:
		flags = unix.AT_REMOVEDIR
	}
	return map[string]any{}, unix.Unlinkat(fd, leaf, flags)
}
