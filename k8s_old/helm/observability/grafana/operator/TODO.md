# Grafana Operator TODOs

## Security Enhancements

### Enable HTTPS between nginx and Traefik

Currently nginx on VPS proxies to k3s via HTTP (port 80). Consider upgrading to HTTPS:

- Change ingress entrypoint from `web` to `websecure` in `templates/ingress-public.yaml`
- Update nginx config to proxy to `https://100.64.0.4:443`
- Add `proxy_ssl_verify off;` to nginx config (or configure proper cert validation)
- Benefits: Defense in depth, even though Tailscale already encrypts the tunnel
- Trade-off: Slightly more complex configuration

### Alternative: Traefik TLS with proper certificates

- Configure cert-manager to issue certificates for internal services
- Use those certificates in Traefik instead of self-signed
- Enable full certificate validation in nginx proxy
