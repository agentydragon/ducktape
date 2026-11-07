package firecracker

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

type Credentials struct {
	TLSCert  string
	TLSKey   string
	CABundle string
}

// ReadCredentials pins all three files to one Kubernetes projected Secret
// generation, even if kubelet flips ..data while the files are being read.
func ReadCredentials(directory string) (Credentials, error) {
	return readCredentialsWith(directory, os.ReadFile)
}

func readCredentialsWith(directory string, readFile func(string) ([]byte, error)) (Credentials, error) {
	generation := filepath.Join(directory, "..data")
	for attempt := 0; attempt < 3; attempt++ {
		root := directory
		info, err := os.Lstat(generation)
		if err == nil && info.Mode()&os.ModeSymlink != 0 {
			root, err = filepath.EvalSymlinks(generation)
			if err != nil {
				if errors.Is(err, os.ErrNotExist) && attempt < 2 {
					continue
				}
				return Credentials{}, err
			}
		} else if err != nil && !errors.Is(err, os.ErrNotExist) {
			return Credentials{}, err
		}

		values := [3]string{}
		complete := true
		for index, name := range []string{"tls.crt", "tls.key", "ca.crt"} {
			data, err := readFile(filepath.Join(root, name))
			if err != nil {
				if errors.Is(err, os.ErrNotExist) && attempt < 2 {
					complete = false
					break
				}
				return Credentials{}, fmt.Errorf("read projected credential %s: %w", name, err)
			}
			values[index] = string(data)
		}
		if complete {
			return Credentials{TLSCert: values[0], TLSKey: values[1], CABundle: values[2]}, nil
		}
	}
	return Credentials{}, fmt.Errorf("projected Secret generation remained incomplete in %s", directory)
}
