# Authenticated OpenAI-compatible access to rugged's loopback-only local inference service.
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.localLlm.bearerProxy;
  mesh = builtins.fromJSON (builtins.readFile ../../../../nebula-mesh.json);
  listenAddress = mesh.hosts.rugged.nebula_ip;
  listenPort = 18080; # cdk8s EndpointSlice targets this listener for LiteLLM.
  backendPort = 11436;
in
{
  options.ducktape.localLlm.bearerProxy = {
    enable = lib.mkEnableOption "bearer-authenticated access to rugged's local Ollama";
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion =
          config.ducktape.localLlm.ollamaUpstream.enable || config.ducktape.localLlm.llamaRouter.enable;
        message = "ducktape.localLlm.bearerProxy requires an enabled local inference backend";
      }
    ];

    sops.secrets.rugged_local_llm_bearer_token = {
      sopsFile = ../../../../cluster/k8s/litellm/secrets/rugged-local-llm-bearer-token.sops.yaml;
      key = "stringData/token";
    };

    # NixOS runs nginx's configuration check and master as this unprivileged
    # user, so the default root-only template cannot be included at startup.
    sops.templates."rugged-local-llm-nginx.conf".owner = config.services.nginx.user;
    sops.templates."rugged-local-llm-nginx.conf".group = config.services.nginx.group;
    sops.templates."rugged-local-llm-nginx.conf".mode = "0400";

    sops.templates."rugged-local-llm-nginx.conf".content = ''
      map $http_authorization $rugged_local_llm_authorized {
        default 0;
        "Bearer ${config.sops.placeholder.rugged_local_llm_bearer_token}" 1;
      }

      server {
        listen ${listenAddress}:${toString listenPort};

        if ($rugged_local_llm_authorized = 0) {
          return 401;
        }

        location = /v1/models {
          limit_except GET {
            deny all;
          }
          proxy_pass http://127.0.0.1:${toString backendPort};
          proxy_http_version 1.1;
          proxy_set_header Host $host;
          proxy_set_header Authorization "";
          proxy_set_header Connection "";
          proxy_buffering off;
          proxy_read_timeout 600s;
          proxy_send_timeout 600s;
        }

        location = /v1/chat/completions {
          limit_except POST {
            deny all;
          }
          proxy_pass http://127.0.0.1:${toString backendPort};
          proxy_http_version 1.1;
          proxy_set_header Host $host;
          proxy_set_header Authorization "";
          proxy_set_header Connection "";
          proxy_buffering off;
          proxy_read_timeout 2400s;
          proxy_send_timeout 600s;
        }

        location / {
          return 404;
        }
      }
    '';
    # Nginx's ExecReload validates the config before signaling workers, so
    # invalid rotated config leaves the current workers using the old token.
    sops.templates."rugged-local-llm-nginx.conf".reloadUnits = [ "nginx.service" ];

    services.nginx = {
      enable = true;
      appendHttpConfig = ''
        # The SOPS-backed bearer map key exceeds nginx's default bucket size.
        map_hash_bucket_size 128;
        include ${config.sops.templates."rugged-local-llm-nginx.conf".path};
      '';
    };

    networking.firewall.interfaces.nebula1.allowedTCPPorts = [ listenPort ];
    systemd.services.nginx = {
      after = [ "nebula.service" ];
      requires = [ "nebula.service" ];
    };
  };
}
