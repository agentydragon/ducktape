# K8s Infrastructure

## Components

### Registry

Container registry for in-cluster Docker images. Accessible at:

- `registry.k3s.agentydragon.com` (Traefik ingress with TLS + Authentik forward-auth)

### cert-manager & TLS Certificates

The cluster uses cert-manager for automatic TLS certificate management with a self-signed homelab CA.

#### Setup

1. **cert-manager v1.13.0** is installed in the `cert-manager` namespace
2. **Homelab CA** - A 10-year self-signed CA certificate for issuing all cluster certificates
3. **ClusterIssuers**:
   - `selfsigned-cluster-issuer` - Bootstrap issuer for creating the CA
   - `homelab-ca-issuer` - Issues certificates signed by the homelab CA

#### Using TLS for Services

To enable HTTPS for any ingress, add these annotations:

```yaml
metadata:
  annotations:
    cert-manager.io/cluster-issuer: "homelab-ca-issuer"
spec:
  tls:
    - hosts:
        - your-service.k3s.local
      secretName: your-service-tls
```

#### Trust the CA on Docker Hosts

To enable Docker to push/pull via HTTPS:

```bash
# Extract the CA certificate
kubectl get secret homelab-ca-secret -n cert-manager -o jsonpath='{.data.ca\.crt}' | base64 -d > homelab-ca.crt

# Install on each Docker host
sudo cp homelab-ca.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates
sudo systemctl restart docker
```

#### Docker Registry Usage

After CA trust is configured:

```bash
# Build and tag
docker build -t registry.k3s.agentydragon.com/myapp:latest .

# Push via HTTPS (Authentik-protected)
docker push registry.k3s.agentydragon.com/myapp:latest

# Use in k8s deployments
image: registry.k3s.agentydragon.com/myapp:latest
```

##### Authenticating with Authentik

The registry ingress sits behind Authentik forward-auth and now accepts HTTP Basic credentials. Every Docker host must log in using an Authentik application password (per-user PAT):

1. In the Authentik UI (`https://auth.k3s.agentydragon.com`), create an _Application password_ under **Account → Security → Application passwords**.
2. On the build host, run:

   ```bash
   docker login registry.k3s.agentydragon.com \
     -u <authentik-username> \
     -p '<application-password>'
   ```

   This writes the base64 credential into `~/.docker/config.json`. If you use a custom `DOCKER_CONFIG`, run the login again with that path so scripts (e.g., `ember/scripts/ember-deploy`) can read the credential.

3. Remove or rotate access by deleting the application password in Authentik; future `docker push` calls will fail until a new password is issued and `docker login` is repeated.

For CI pipelines, keep the Authentik password in your secrets manager and feed it to `docker login --password-stdin`.

### Observability

- OpenAI probe for API monitoring
- TimescaleDB for metrics storage
- (Previously: Loki, Grafana, Promtail - archived)

### Infrastructure

- MetalLB for LoadBalancer services
- Traefik as ingress controller (LoadBalancer IP: 10.0.200.100)

All high-level workloads have first-party Helm charts (`k8s/helm/`) and are orchestrated together via Helmfile (`k8s/helmfile/helmfile.yaml`). Use `helmfile apply` from that directory to roll the full stack once secrets and container images are in place.

## DNS Configuration

All `*.k3s.agentydragon.com` domains resolve to the Traefik LoadBalancer at 10.0.200.100 (via PowerDNS on the VPS).

### Authenticating with Authentik

1. In the Authentik UI (`https://auth.k3s.agentydragon.com`), open **Account → Security → Application passwords**.
2. Hit **Create**, give it an identifier (e.g. `docker-registry`), optionally add a description, and copy the generated password.
3. Log in to the registry once:

   ```bash
   docker login registry.k3s.agentydragon.com \
     -u <authentik-username> \
     -p '<application-password>'
   ```

   Docker writes the credential to `~/.docker/config.json`; if you run scripts with `DOCKER_CONFIG`, repeat the login for that directory.

4. Rotate by deleting the old application password in Authentik and issuing a new one; subsequent `docker push` calls will fail until you log in again.
