// Docker exec helpers for scratch containers in Go.
//
// Creates an isolated container (network disabled) with `sleep infinity`,
// then runs commands inside it via the Docker exec API. Used by the guesser
// agent for scratch computation during the game.
package main

import (
	"bytes"
	"context"
	"fmt"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"
	"github.com/docker/docker/pkg/stdcopy"
)

// ScratchContainer holds a running scratch container's state.
type ScratchContainer struct {
	client      *client.Client
	containerID string
}

// CreateScratchContainer creates and starts a container running `sleep infinity`
// with networking disabled. The caller must call Remove when done.
func CreateScratchContainer(ctx context.Context, image string) (*ScratchContainer, error) {
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return nil, fmt.Errorf("creating docker client: %w", err)
	}

	resp, err := cli.ContainerCreate(
		ctx,
		&container.Config{
			Image: image,
			Cmd:   []string{"sleep", "infinity"},
			Tty:   false,
		},
		&container.HostConfig{
			NetworkMode: "none",
		},
		nil, // networking config
		nil, // platform
		"",  // auto-generated name
	)
	if err != nil {
		cli.Close()
		return nil, fmt.Errorf("creating container: %w", err)
	}

	if err := cli.ContainerStart(ctx, resp.ID, container.StartOptions{}); err != nil {
		_ = cli.ContainerRemove(ctx, resp.ID, container.RemoveOptions{Force: true})
		cli.Close()
		return nil, fmt.Errorf("starting container: %w", err)
	}

	return &ScratchContainer{client: cli, containerID: resp.ID}, nil
}

// ExecResult holds the output of a command executed in a scratch container.
type ExecResult struct {
	Output   string
	ExitCode int
}

// Exec runs a command inside the scratch container with a timeout.
func (sc *ScratchContainer) Exec(ctx context.Context, cmd []string, cwd string, timeoutMs int) (*ExecResult, error) {
	if timeoutMs <= 0 {
		timeoutMs = 10000
	}
	execCtx, cancel := context.WithTimeout(ctx, time.Duration(timeoutMs)*time.Millisecond)
	defer cancel()

	workDir := cwd
	if workDir == "" {
		workDir = "/work"
	}

	execCfg := container.ExecOptions{
		Cmd:          cmd,
		WorkingDir:   workDir,
		AttachStdout: true,
		AttachStderr: true,
	}

	execID, err := sc.client.ContainerExecCreate(execCtx, sc.containerID, execCfg)
	if err != nil {
		return nil, fmt.Errorf("creating exec: %w", err)
	}

	attach, err := sc.client.ContainerExecAttach(execCtx, execID.ID, container.ExecAttachOptions{})
	if err != nil {
		return nil, fmt.Errorf("attaching exec: %w", err)
	}
	defer attach.Close()

	var stdout, stderr bytes.Buffer
	if _, err = stdcopy.StdCopy(&stdout, &stderr, attach.Reader); err != nil {
		if execCtx.Err() != nil {
			return &ExecResult{
				Output:   stdout.String() + stderr.String() + "\n[timed out]",
				ExitCode: -1,
			}, nil
		}
		return nil, fmt.Errorf("reading exec output: %w", err)
	}

	// Use the original (non-timeout) context for inspect since the exec already completed.
	inspect, err := sc.client.ContainerExecInspect(ctx, execID.ID)
	if err != nil {
		return nil, fmt.Errorf("inspecting exec: %w", err)
	}

	output := stdout.String()
	if stderr.Len() > 0 {
		output += stderr.String()
	}

	return &ExecResult{
		Output:   output,
		ExitCode: inspect.ExitCode,
	}, nil
}

// Remove stops and removes the scratch container and closes the Docker client.
func (sc *ScratchContainer) Remove(ctx context.Context) error {
	timeout := 5 // seconds
	stopOpts := container.StopOptions{Timeout: &timeout}
	_ = sc.client.ContainerStop(ctx, sc.containerID, stopOpts)
	err := sc.client.ContainerRemove(ctx, sc.containerID, container.RemoveOptions{Force: true})
	sc.client.Close()
	return err
}
