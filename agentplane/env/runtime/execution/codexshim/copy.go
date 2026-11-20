package codexshim

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
)

func copyRequest(p params) (any, error) {
	src, err := localPath(p.SourcePath)
	if err != nil {
		return nil, err
	}
	dst, err := localPath(p.DestinationPath)
	if err != nil {
		return nil, err
	}
	info, err := os.Lstat(src)
	if err != nil {
		return nil, err
	}
	if info.IsDir() {
		if p.Recursive == nil || !*p.Recursive {
			return nil, errors.New("directory copy requires recursive")
		}
		source, err := filepath.EvalSymlinks(src)
		if err != nil {
			return nil, err
		}
		parent, err := filepath.EvalSymlinks(filepath.Dir(dst))
		if err != nil {
			return nil, err
		}
		dest := filepath.Join(parent, filepath.Base(dst))
		rel, err := filepath.Rel(source, dest)
		if err != nil {
			return nil, err
		}
		if rel == "." || (rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))) {
			return nil, errors.New("destination is within source")
		}
	}
	return map[string]any{}, copyEntry(src, dst)
}

func copyEntry(src, dst string) error {
	info, err := os.Lstat(src)
	if err != nil {
		return err
	}
	switch {
	case info.Mode()&os.ModeSymlink != 0:
		target, err := os.Readlink(src)
		if err != nil {
			return err
		}
		return os.Symlink(target, dst)
	case info.IsDir():
		if err := os.MkdirAll(dst, info.Mode().Perm()); err != nil {
			return err
		}
		entries, err := os.ReadDir(src)
		if err != nil {
			return err
		}
		for _, entry := range entries {
			if err := copyEntry(filepath.Join(src, entry.Name()), filepath.Join(dst, entry.Name())); err != nil {
				return err
			}
		}
		return nil
	case info.Mode().IsRegular():
		input, err := os.Open(src)
		if err != nil {
			return err
		}
		defer input.Close()
		output, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, info.Mode().Perm())
		if err != nil {
			return err
		}
		_, copyErr := io.Copy(output, input)
		closeErr := output.Close()
		return errors.Join(copyErr, closeErr)
	default:
		return errors.New("unsupported source type")
	}
}
