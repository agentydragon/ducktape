package systemd

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	goSystemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/godbus/dbus/v5"
	"github.com/google/uuid"
)

type execStart struct {
	Path      string
	Arguments []string
	Flags     []string
}

type unitCondition struct {
	Type            string
	Trigger, Negate bool
	Parameter       string
}

// BackendConfig is the immutable systemd and launch configuration for one
// native process attempt.
type BackendConfig struct {
	Command        []string
	Environment    []string
	Dir            string
	StopGrace      time.Duration
	Scope          string
	Slice          string
	LifetimeMarker string
	ParentUnit     string
	LaunchReady    func() bool
	Properties     func() ([]goSystemd.Property, error)
	Identity       []goSystemd.Property
	OpenConnection func() (*goSystemd.Conn, context.CancelFunc, error)
	StartTransient func(context.Context, *goSystemd.Conn, string, []goSystemd.Property, chan<- string) (int, error)
	ReapSnapshot   func(context.Context, *goSystemd.Conn, string) (UnitSnapshot, error)
	StopUnit       func(context.Context, *goSystemd.Conn, string) (<-chan string, error)
	ReferenceUnit  func(context.Context, string) (UnitReference, error)
	Property       func(string, any) goSystemd.Property
}

// Backend creates a fresh, uniquely named systemd unit for each attempt.
type Backend struct{ config BackendConfig }

func NewBackend(config BackendConfig) *Backend { return &Backend{config: config} }

func closeFiles(files ...*os.File) {
	for _, file := range files {
		if file != nil {
			_ = file.Close()
		}
	}
}

func (b *Backend) Validate() error {
	if len(b.config.Command) == 0 || !filepath.IsAbs(b.config.Command[0]) {
		return errors.New("VM native executable must be absolute")
	}
	return nil
}

// Start returns a typed owner before the manager request begins. WaitStarted
// observes accepted process startup; Stop and Wait remain valid if that result
// is unconfirmed.
func (b *Backend) Start(ctx context.Context) (*StartHandle, error) {
	if err := b.Validate(); err != nil {
		return nil, err
	}
	if b.config.LaunchReady != nil && !b.config.LaunchReady() {
		return nil, errors.New("systemd scope is not ready")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if b.config.Scope == "" {
		return nil, errors.New("VM process launcher has not fenced its owner scope")
	}
	if b.config.LifetimeMarker == "" {
		return nil, errors.New("VM process launcher has no Environment Runtime invocation guard")
	}
	if b.config.OpenConnection == nil {
		return nil, errors.New("systemd connection opener is not configured")
	}
	conn, cancelConnection, err := b.config.OpenConnection()
	if err != nil {
		return nil, fmt.Errorf("connect native systemd manager: %w", err)
	}
	closeConnection := func() {
		conn.Close()
		cancelConnection()
	}
	stdinReader, stdinWriter, err := os.Pipe()
	if err != nil {
		closeConnection()
		return nil, err
	}
	stdoutReader, stdoutWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter)
		closeConnection()
		return nil, err
	}
	stderrReader, stderrWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter)
		closeConnection()
		return nil, err
	}
	if b.config.Properties == nil || b.config.Property == nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter)
		closeConnection()
		return nil, errors.New("systemd unit properties are not configured")
	}
	properties, err := b.config.Properties()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter)
		closeConnection()
		return nil, err
	}
	properties = append(properties, b.config.Identity...)
	name := b.config.Scope + uuid.NewString() + ".service"
	properties = append(
		properties,
		b.config.Property("DefaultDependencies", false),
		b.config.Property("Description", "Agentplane native launch "+name),
		b.config.Property("Type", "exec"),
		b.config.Property("Restart", "no"),
		b.config.Property("Slice", b.config.Slice),
		b.config.Property("BindsTo", []string{b.config.ParentUnit}),
		b.config.Property("After", []string{b.config.ParentUnit}),
		b.config.Property("RemainAfterExit", true),
		b.config.Property("CollectMode", "inactive"),
		b.config.Property("Conditions", []unitCondition{{Type: "ConditionPathExists", Parameter: b.config.LifetimeMarker}}),
		b.config.Property("AddRef", true),
		b.config.Property("ExecStartEx", []execStart{{Path: b.config.Command[0], Arguments: b.config.Command, Flags: []string{"no-env-expand"}}}),
		b.config.Property("Environment", b.config.Environment),
		b.config.Property("WorkingDirectory", b.config.Dir),
		b.config.Property("StandardInputFileDescriptor", dbus.UnixFD(stdinReader.Fd())),
		b.config.Property("StandardOutputFileDescriptor", dbus.UnixFD(stdoutWriter.Fd())),
		b.config.Property("StandardErrorFileDescriptor", dbus.UnixFD(stderrWriter.Fd())),
		b.config.Property("PrivateTmp", false),
		b.config.Property("ProtectSystem", "strict"),
		b.config.Property("ReadWritePaths", []string{"/state", "/tmp"}),
		b.config.Property("UMask", uint32(7)),
		b.config.Property("LockPersonality", true),
		b.config.Property("ProtectKernelModules", true),
		b.config.Property("ProtectKernelLogs", true),
		b.config.Property("KillMode", "control-group"),
		b.config.Property("KillSignal", int32(15)),
		b.config.Property("SendSIGKILL", true),
		b.config.Property("TimeoutStopUSec", systemdStopTimeout(b.config.StopGrace)),
		b.config.Property("OOMPolicy", "continue"),
	)

	handle := newStartHandle(b, conn, cancelConnection, name, stdinWriter, stdoutReader, stderrReader)
	handle.observeStart(StartPreparing, nil)
	handle.updates = make(chan *goSystemd.PropertiesUpdate, 32)
	handle.updateErrors = make(chan error, 1)
	conn.SetPropertiesSubscriber(handle.updates, handle.updateErrors)
	go handle.runStart(ctx, properties, stdinReader, stdoutWriter, stderrWriter)

	return handle, nil
}
