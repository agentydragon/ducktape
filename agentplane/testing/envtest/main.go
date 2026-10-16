package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

func main() {
	if err := run(); err != nil {
		log.Printf("envtest server: %v", err)
		os.Exit(1)
	}
}

func run() (resultErr error) {
	flags := flag.NewFlagSet("envtest", flag.ContinueOnError)
	assets := flags.String("assets", "", "directory containing kube-apiserver, etcd, and kubectl")
	crds := flags.String("crds", "", "directory containing CRD YAML or JSON files to install")
	kubeconfig := flags.String("kubeconfig", "", "path to create the kubeconfig after the API server is ready")
	if err := flags.Parse(os.Args[1:]); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected positional arguments")
	}
	if *assets == "" {
		return fmt.Errorf("--assets is required")
	}
	if *crds == "" {
		return fmt.Errorf("--crds is required")
	}
	if *kubeconfig == "" {
		return fmt.Errorf("--kubeconfig is required")
	}
	if err := os.Setenv("KUBEBUILDER_ASSETS", *assets); err != nil {
		return fmt.Errorf("set pinned envtest asset directory: %w", err)
	}

	ctx, stopSignals := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopSignals()

	environment := &envtest.Environment{
		BinaryAssetsDirectory:    *assets,
		CRDDirectoryPaths:        []string{*crds},
		DownloadBinaryAssets:     false,
		ErrorIfCRDPathMissing:    true,
		AttachControlPlaneOutput: true,
		UseExistingCluster:       new(false),
	}
	if _, err := environment.Start(); err != nil {
		if stopErr := environment.Stop(); stopErr != nil {
			return errors.Join(fmt.Errorf("start envtest control plane: %w", err), fmt.Errorf("stop partially started control plane: %w", stopErr))
		}
		return fmt.Errorf("start envtest control plane: %w", err)
	}
	defer func() {
		if err := environment.Stop(); err != nil {
			resultErr = errors.Join(resultErr, fmt.Errorf("stop envtest control plane: %w", err))
		}
	}()

	if ctx.Err() != nil {
		return nil
	}
	if err := writeKubeConfig(*kubeconfig, environment.KubeConfig); err != nil {
		return fmt.Errorf("write ready envtest kubeconfig: %w", err)
	}

	<-ctx.Done()
	return nil
}

func writeKubeConfig(path string, data []byte) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".envtest-kubeconfig-*")
	if err != nil {
		return fmt.Errorf("create temporary kubeconfig: %w", err)
	}
	temporaryPath := file.Name()
	defer os.Remove(temporaryPath)

	if _, err := file.Write(data); err != nil {
		file.Close()
		return fmt.Errorf("write temporary kubeconfig: %w", err)
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return fmt.Errorf("sync temporary kubeconfig: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close temporary kubeconfig: %w", err)
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return fmt.Errorf("publish kubeconfig atomically: %w", err)
	}
	return nil
}
