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

	"golang.org/x/sync/errgroup"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
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

	if err := listEveryCRD(ctx, environment); err != nil && ctx.Err() == nil {
		return fmt.Errorf("list every installed CRD: %w", err)
	}
	if ctx.Err() != nil {
		return nil
	}
	if err := writeKubeConfig(*kubeconfig, environment.KubeConfig); err != nil {
		return fmt.Errorf("write ready envtest kubeconfig: %w", err)
	}

	<-ctx.Done()
	return nil
}

// listEveryCRD lists each served version of each installed CRD once. The API server builds a CRD's
// storage at the first request for it and answers lists 429 "storage is (re)initializing" until
// that storage's watch cache has synced. client-go waits that out; the Python client the fixtures
// hand to tests does not, so the server is ready only once every CRD has listed.
func listEveryCRD(ctx context.Context, environment *envtest.Environment) error {
	cluster, err := client.New(environment.Config, client.Options{})
	if err != nil {
		return fmt.Errorf("create client: %w", err)
	}
	lists, ctx := errgroup.WithContext(ctx)
	for _, crd := range environment.CRDs {
		for _, version := range crd.Spec.Versions {
			if !version.Served {
				continue
			}
			lists.Go(func() error {
				list := &unstructured.UnstructuredList{}
				list.SetGroupVersionKind(schema.GroupVersionKind{
					Group:   crd.Spec.Group,
					Version: version.Name,
					Kind:    crd.Spec.Names.Kind + "List",
				})
				if err := cluster.List(ctx, list, client.Limit(1)); err != nil {
					return fmt.Errorf("list %s %s: %w", crd.Name, version.Name, err)
				}
				return nil
			})
		}
	}
	return lists.Wait()
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
