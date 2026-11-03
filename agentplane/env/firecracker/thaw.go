package firecracker

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const maxThawMessageBytes = 1 << 20

type thawMessage struct {
	UnixNano int64  `json:"unix_nano"`
	TLSCert  string `json:"tls_cert"`
	TLSKey   string `json:"tls_key"`
	CABundle string `json:"ca_bundle"`
}

type thawAcknowledgement struct {
	BootID string `json:"boot_id"`
}

type credentialFiles struct {
	TLSCertPath  string
	TLSKeyPath   string
	CABundlePath string
}

type clockSetter func(time.Time) error

func validateThawMessage(message thawMessage) error {
	if message.UnixNano <= 0 {
		return errors.New("unix_nano must be positive")
	}
	if message.TLSCert == "" || message.TLSKey == "" || message.CABundle == "" {
		return errors.New("tls_cert, tls_key, and ca_bundle must be non-empty")
	}
	if _, err := tls.X509KeyPair([]byte(message.TLSCert), []byte(message.TLSKey)); err != nil {
		return fmt.Errorf("parse runtime TLS certificate and key: %w", err)
	}
	trust := x509.NewCertPool()
	if !trust.AppendCertsFromPEM([]byte(message.CABundle)) {
		return errors.New("ca_bundle contains no certificates")
	}
	return nil
}

func readThawMessage(reader io.Reader) (thawMessage, error) {
	buffered := bufio.NewReader(reader)
	var line []byte
	for {
		fragment, err := buffered.ReadSlice('\n')
		if len(line)+len(fragment) > maxThawMessageBytes {
			return thawMessage{}, fmt.Errorf("thaw message exceeds %d bytes", maxThawMessageBytes)
		}
		line = append(line, fragment...)
		if err == nil {
			break
		}
		if !errors.Is(err, bufio.ErrBufferFull) {
			if errors.Is(err, io.EOF) {
				return thawMessage{}, errors.New("thaw message must end with a newline")
			}
			return thawMessage{}, fmt.Errorf("read thaw message: %w", err)
		}
	}
	if buffered.Buffered() != 0 {
		return thawMessage{}, errors.New("thaw connection contains data after its newline message")
	}
	decoder := json.NewDecoder(bytes.NewReader(bytes.TrimSuffix(line, []byte{'\n'})))
	decoder.DisallowUnknownFields()
	var message thawMessage
	if err := decoder.Decode(&message); err != nil {
		return thawMessage{}, fmt.Errorf("decode thaw message: %w", err)
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		if err == nil {
			return thawMessage{}, errors.New("thaw message contains multiple JSON values")
		}
		return thawMessage{}, fmt.Errorf("decode trailing thaw data: %w", err)
	}
	if err := validateThawMessage(message); err != nil {
		return thawMessage{}, err
	}
	return message, nil
}

func readSeed(seedDir string) (thawMessage, error) {
	paths := []string{"tls.crt", "tls.key", "ca-certificates.crt"}
	values := make([]string, len(paths))
	for index, name := range paths {
		path := filepath.Join(seedDir, name)
		info, err := os.Lstat(path)
		if err != nil {
			return thawMessage{}, fmt.Errorf("inspect credential seed %q: %w", path, err)
		}
		if !info.Mode().IsRegular() {
			return thawMessage{}, fmt.Errorf("credential seed %q is not a regular file", path)
		}
		contents, err := os.ReadFile(path)
		if err != nil {
			return thawMessage{}, fmt.Errorf("read credential seed %q: %w", path, err)
		}
		if len(contents) > maxThawMessageBytes {
			return thawMessage{}, fmt.Errorf("credential seed %q exceeds %d bytes", path, maxThawMessageBytes)
		}
		values[index] = string(contents)
	}
	message := thawMessage{TLSCert: values[0], TLSKey: values[1], CABundle: values[2]}
	if err := validateThawMessage(thawMessage{
		UnixNano: time.Now().UnixNano(), TLSCert: message.TLSCert, TLSKey: message.TLSKey, CABundle: message.CABundle,
	}); err != nil {
		return thawMessage{}, fmt.Errorf("validate credential seed: %w", err)
	}
	return message, nil
}

func writeAtomic(path string, contents []byte, mode os.FileMode) error {
	directory := filepath.Dir(path)
	file, err := os.CreateTemp(directory, ".agentplane-thaw-*")
	if err != nil {
		return fmt.Errorf("create temporary credential in %q: %w", directory, err)
	}
	temporary := file.Name()
	defer os.Remove(temporary)
	if err := file.Chmod(mode); err != nil {
		_ = file.Close()
		return fmt.Errorf("set mode on temporary credential %q: %w", temporary, err)
	}
	if _, err := file.Write(contents); err != nil {
		_ = file.Close()
		return fmt.Errorf("write temporary credential %q: %w", temporary, err)
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return fmt.Errorf("sync temporary credential %q: %w", temporary, err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close temporary credential %q: %w", temporary, err)
	}
	if err := os.Rename(temporary, path); err != nil {
		return fmt.Errorf("install credential %q: %w", path, err)
	}
	directoryFile, err := os.Open(directory)
	if err != nil {
		return fmt.Errorf("open credential directory %q: %w", directory, err)
	}
	defer directoryFile.Close()
	if err := directoryFile.Sync(); err != nil {
		return fmt.Errorf("sync credential directory %q: %w", directory, err)
	}
	return nil
}

func applyCredentials(files credentialFiles, message thawMessage) error {
	if err := writeAtomic(files.TLSCertPath, []byte(message.TLSCert), 0o600); err != nil {
		return err
	}
	if err := writeAtomic(files.TLSKeyPath, []byte(message.TLSKey), 0o600); err != nil {
		return err
	}
	if err := writeAtomic(files.CABundlePath, []byte(message.CABundle), 0o644); err != nil {
		return err
	}
	return nil
}

func handleThaw(
	connection io.ReadWriter,
	peerCID uint32,
	hostCID uint32,
	bootID string,
	files credentialFiles,
	setClock clockSetter,
	onFirstThaw func() error,
	ready *bool,
) error {
	if peerCID != hostCID {
		return fmt.Errorf("reject vsock peer CID %d; only host CID %d is allowed", peerCID, hostCID)
	}
	if strings.TrimSpace(bootID) == "" {
		return errors.New("boot_id is empty")
	}
	message, err := readThawMessage(connection)
	if err != nil {
		return err
	}
	if err := setClock(time.Unix(0, message.UnixNano)); err != nil {
		return fmt.Errorf("set guest clock: %w", err)
	}
	if err := applyCredentials(files, message); err != nil {
		return err
	}
	if !*ready {
		if err := onFirstThaw(); err != nil {
			return fmt.Errorf("notify systemd that thaw is ready: %w", err)
		}
		*ready = true
	}
	acknowledgement, err := json.Marshal(thawAcknowledgement{BootID: bootID})
	if err != nil {
		return fmt.Errorf("encode thaw acknowledgement: %w", err)
	}
	acknowledgement = append(acknowledgement, '\n')
	if _, err := connection.Write(acknowledgement); err != nil {
		return fmt.Errorf("write thaw acknowledgement: %w", err)
	}
	return nil
}

func notifySystemdReady() error {
	address := os.Getenv("NOTIFY_SOCKET")
	if address == "" {
		return errors.New("NOTIFY_SOCKET is unset")
	}
	connection, err := net.DialUnix("unixgram", nil, &net.UnixAddr{Name: address, Net: "unixgram"})
	if err != nil {
		return fmt.Errorf("connect to systemd notify socket %q: %w", address, err)
	}
	defer connection.Close()
	if _, err := connection.Write([]byte("READY=1\nSTATUS=host thaw accepted; Runtime may start\n")); err != nil {
		return fmt.Errorf("notify systemd readiness: %w", err)
	}
	return nil
}

func serve(ctx context.Context, seedDir string, runtimeTLSDir string, runtimeTrustDir string) error {
	if os.Geteuid() != 0 {
		return errors.New("firecracker thaw agent must run as root")
	}
	if err := os.MkdirAll(runtimeTLSDir, 0o700); err != nil {
		return fmt.Errorf("create private runtime TLS directory: %w", err)
	}
	if err := os.Chmod(runtimeTLSDir, 0o700); err != nil {
		return fmt.Errorf("set private runtime TLS directory mode: %w", err)
	}
	if err := os.MkdirAll(runtimeTrustDir, 0o755); err != nil {
		return fmt.Errorf("create runtime trust directory: %w", err)
	}
	if err := os.Chmod(runtimeTrustDir, 0o755); err != nil {
		return fmt.Errorf("set runtime trust directory mode: %w", err)
	}
	seed, err := readSeed(seedDir)
	if err != nil {
		return err
	}
	files := credentialFiles{
		TLSCertPath:  filepath.Join(runtimeTLSDir, "tls.crt"),
		TLSKeyPath:   filepath.Join(runtimeTLSDir, "tls.key"),
		CABundlePath: filepath.Join(runtimeTrustDir, "ca-certificates.crt"),
	}
	if err := applyCredentials(files, seed); err != nil {
		return fmt.Errorf("install initial credential seed: %w", err)
	}
	bootIDBytes, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return fmt.Errorf("read kernel boot ID: %w", err)
	}
	bootID := strings.TrimSpace(string(bootIDBytes))
	if bootID == "" {
		return errors.New("kernel boot ID is empty")
	}
	return serveVsock(ctx, 52, bootID, files, setRealtimeClock, notifySystemdReady)
}

func Run(ctx context.Context) error {
	return serve(
		ctx,
		"/run/agentplane/credential-seed",
		"/run/agentplane/environment-runtime-private/environment-runtime-tls",
		"/run/agentplane/environment-runtime-trust",
	)
}
