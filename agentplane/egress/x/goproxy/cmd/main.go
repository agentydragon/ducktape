// A loopback-only lab proxy for the shared socket contracts. No deployment uses it.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/netip"
	"os"

	spike "github.com/agentydragon/ducktape/agentplane/egress/x/goproxy"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	authentication "k8s.io/client-go/kubernetes/typed/authentication/v1"
	"k8s.io/client-go/tools/clientcmd"
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	snapshot := flag.String("snapshot", "", "JSON lab Snapshot; re-read per admission (contains fixture secrets)")
	ca := flag.String("ca", "", "interception CA PEM")
	key := flag.String("key", "", "interception CA private key PEM")
	roots := flag.String("roots", "", "upstream CA PEM bundle, independent of interception CA")
	kubeconfig := flag.String("kubeconfig", "", "TokenReview API kubeconfig")
	audience := flag.String("audience", "", "required TokenReview audience")
	namespace := flag.String("namespace", "", "one accepted workload namespace")
	port := flag.Int("port", 0, "loopback listener port (0 chooses a port)")
	host := flag.String("fixture-host", "", "one lab DNS name mapped to 127.0.0.1")
	flag.Parse()
	certificate, err := tls.LoadX509KeyPair(*ca, *key)
	if err != nil {
		return err
	}
	pem, err := os.ReadFile(*roots)
	if err != nil {
		return err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return fmt.Errorf("no upstream roots")
	}
	config, err := clientcmd.BuildConfigFromFlags("", *kubeconfig)
	if err != nil {
		return err
	}
	reviews, err := authentication.NewForConfig(config)
	if err != nil {
		return err
	}
	resolver, err := auth.NewResolver(reviews.TokenReviews(), *audience, []string{*namespace})
	if err != nil {
		return err
	}
	proxy, err := spike.New(spike.Options{
		CA: certificate, Roots: pool, Auth: resolver,
		ReadSnapshot: func() (spike.Snapshot, error) {
			file, err := os.Open(*snapshot)
			if err != nil {
				return spike.Snapshot{}, err
			}
			defer file.Close()
			decoder := json.NewDecoder(file)
			decoder.DisallowUnknownFields()
			var state spike.Snapshot
			err = decoder.Decode(&state)
			return state, err
		},
		Resolve: func(ctx context.Context, network, name string) ([]netip.Addr, error) {
			if *host != "" && name == *host {
				return []netip.Addr{netip.MustParseAddr("127.0.0.1")}, nil
			}
			return net.DefaultResolver.LookupNetIP(ctx, network, name)
		},
		Exempt: []netip.Prefix{netip.MustParsePrefix("127.0.0.1/32")},
	})
	if err != nil {
		return err
	}
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", *port))
	if err != nil {
		return err
	}
	if err := json.NewEncoder(os.Stdout).Encode(map[string]int{"port": listener.Addr().(*net.TCPAddr).Port}); err != nil {
		return err
	}
	return http.Serve(listener, proxy)
}
