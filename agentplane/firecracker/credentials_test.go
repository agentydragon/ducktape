package firecracker

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadCredentialsKeepsProjectedGenerationAcrossSymlinkFlip(t *testing.T) {
	directory := t.TempDir()
	for _, version := range []string{"old", "new"} {
		root := filepath.Join(directory, version)
		if err := os.Mkdir(root, 0o700); err != nil {
			t.Fatal(err)
		}
		for _, name := range []string{"tls.crt", "tls.key", "ca.crt"} {
			if err := os.WriteFile(filepath.Join(root, name), []byte(version+"-"+name), 0o600); err != nil {
				t.Fatal(err)
			}
		}
	}
	generation := filepath.Join(directory, "..data")
	if err := os.Symlink("old", generation); err != nil {
		t.Fatal(err)
	}

	flipped := false
	credentials, err := readCredentialsWith(directory, func(path string) ([]byte, error) {
		data, err := os.ReadFile(path)
		if err == nil && !flipped && path == filepath.Join(directory, "old", "tls.crt") {
			replacement := filepath.Join(directory, "..data-next")
			if err := os.Symlink("new", replacement); err != nil {
				return nil, err
			}
			if err := os.Rename(replacement, generation); err != nil {
				return nil, err
			}
			flipped = true
		}
		return data, err
	})
	if err != nil {
		t.Fatal(err)
	}
	if !flipped || credentials != (Credentials{
		TLSCert:  "old-tls.crt",
		TLSKey:   "old-tls.key",
		CABundle: "old-ca.crt",
	}) {
		t.Fatalf("credential generation changed during read: %+v", credentials)
	}
}

func TestReadCredentialsSupportsDirectDirectory(t *testing.T) {
	directory := t.TempDir()
	for _, name := range []string{"tls.crt", "tls.key", "ca.crt"} {
		if err := os.WriteFile(filepath.Join(directory, name), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	credentials, err := ReadCredentials(directory)
	if err != nil {
		t.Fatal(err)
	}
	if credentials != (Credentials{}) {
		t.Fatalf("empty files were not preserved: %+v", credentials)
	}
}
