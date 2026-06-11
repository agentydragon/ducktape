# prometheus-nginxlog-exporter config for the wayback-cache sidecar.
# nginx streams the lean `wayback_metrics` access line over loopback syslog
# (udp 127.0.0.1:5140); this parses it into Prometheus metrics scraped at :4040.
#
# Emitted (namespace "wayback"):
#   wayback_http_response_count_total{method,status,origin,cache,up,...} — request counts.
#     * cache hit rate  = ratio of {cache="HIT"} to total.
#     * IA TCP refusals = {status="502", up=~"-.*"} (no upstream HTTP response).
#     * IA HTTP errors  = {status="502", up!~"-.*"} etc.
#   wayback_http_response_time_seconds{...} — request-duration histogram.
listen {
  port = 4040
  address = "0.0.0.0"
  metrics_endpoint = "/metrics"
}

namespace "wayback" {
  format = "$remote_addr \"$request\" $status origin=$wayback_cache_origin cache=$upstream_cache_status up=\"$upstream_status\" rt=$request_time xrl=\"$upstream_http_x_rl\" retry_after=\"$upstream_http_retry_after\" xna=\"$upstream_http_x_na\""

  source {
    syslog {
      listen_address = "udp://127.0.0.1:5140"
      format = "rfc3164"
      tags = ["wayback"]
    }
  }

  # Promote low-cardinality custom fields to metric labels.
  relabel "origin" {
    from = "wayback_cache_origin"
  }
  relabel "cache" {
    from = "upstream_cache_status"
  }
  relabel "up" {
    from = "upstream_status"
  }
  relabel "xrl" {
    from = "upstream_http_x_rl"
  }
  relabel "retry_after" {
    from = "upstream_http_retry_after"
  }
  relabel "xna" {
    from = "upstream_http_x_na"
  }
}
