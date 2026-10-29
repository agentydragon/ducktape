module github.com/agentydragon/ducktape

go 1.26.2

// Direct dependencies for the runner's Claude protocol projection
require (
	github.com/anthropics/anthropic-sdk-go v1.23.0
	github.com/schlunsen/claude-agent-sdk-go v0.13.0
)

// Direct dependencies for skills/.../genkit (20q eval)
require (
	github.com/docker/docker v28.5.2+incompatible
	github.com/firebase/genkit/go v1.13.1
)

// Direct dependencies for finance/worthy
require github.com/fsouza/go-dockerclient v1.13.3

// Direct dependencies for Agentplane Kubernetes authentication.
require (
	k8s.io/apimachinery v0.37.0
	k8s.io/apiserver v0.37.0
)

// Direct dependencies for agentplane/policy_controller
require (
	k8s.io/api v0.37.0
	k8s.io/client-go v0.37.0
	sigs.k8s.io/controller-runtime v0.25.2
)

// Direct dependencies for agentplane/sandbox_controller
require kubevirt.io/api v1.8.2

// Direct dependencies for agentplane/runner_gateway
require github.com/go-logr/logr v1.4.4

// Direct dependencies for agentplane/runner lifecycle orchestration
require (
	github.com/cosi-project/runtime v1.16.2
	github.com/siderolabs/gen v0.8.6
	go.uber.org/zap v1.28.0
)

// Direct dependency for the central sampler's isolated JavaScript host
require github.com/containerd/cgroups/v3 v3.1.3

// Direct dependency for the central sampler's view_image decoding and resizing
require golang.org/x/image v0.46.0

// Direct dependencies for Agentplane integration-test fixtures
require (
	github.com/moby/moby/client v0.5.1
	github.com/testcontainers/testcontainers-go v0.44.0
)

// Direct dependencies for Agentplane service configuration
require (
	github.com/go-viper/mapstructure/v2 v2.5.0
	github.com/spf13/viper v1.21.0
)

// Direct dependencies for the Agentplane BFF HTTP API.
require github.com/danielgtaylor/huma/v2 v2.39.1

// Direct dependencies for the generated HTTP facades of agentplane services
require github.com/grpc-ecosystem/grpc-gateway/v2 v2.30.0

// Direct dependencies for Agentplane YAML consumers
require gopkg.in/yaml.v3 v3.0.1

// Direct dependencies for agentplane/egress/go
require (
	code.dny.dev/ssrf v0.3.0
	github.com/go-json-experiment/json v0.0.0-20260820222146-c27c302e5fc3
	github.com/invopop/jsonschema v0.14.0
)

// Direct dependencies for agentplane/action_service/policies
require github.com/santhosh-tekuri/jsonschema/v6 v6.0.2

// Direct dependencies for agentplane/notification_service/github
require (
	github.com/bradleyfalzon/ghinstallation/v2 v2.19.0
	github.com/google/go-github/v88 v88.0.0
)

// Direct dependencies for agentplane/mcp_edge
require github.com/modelcontextprotocol/go-sdk v1.8.0

// Direct dependencies for agentplane/action_service/api (operator OIDC tokens)
require (
	github.com/coreos/go-oidc/v3 v3.21.0
	github.com/go-jose/go-jose/v4 v4.1.4
)

// Direct dependencies for agentplane/action_service/mcpexec (bearer tokens sent to MCP servers)
require golang.org/x/oauth2 v0.37.0

// Direct dependencies for agentplane/action_service/push (VAPID and RFC 8291 payload encryption)
require (
	github.com/SherClockHolmes/webpush-go v1.4.0
	github.com/golang-jwt/jwt/v5 v5.3.1 // indirect
)

// Direct dependencies for agentplane/mcp_edge/authserver (the OAuth authorization server external MCP clients use)
// Gotcha: op.Server and op.RegisterServer "may change until v4", in a v3 minor release too.
require github.com/zitadel/oidc/v3 v3.51.16

// Indirect dependencies (merged from genkit and worthy go.mods)
require (
	github.com/Azure/go-ansiterm v0.0.0-20250102033503-faa5f7b0171c // indirect
	github.com/Microsoft/go-winio v0.6.2 // indirect
	github.com/bahlo/generic-list-go v0.2.0 // indirect
	github.com/blang/semver/v4 v4.0.0 // indirect
	github.com/buger/jsonparser v1.1.2 // indirect
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/containerd/errdefs v1.0.0 // indirect
	github.com/containerd/errdefs/pkg v0.3.0 // indirect
	github.com/containerd/log v0.1.0 // indirect
	github.com/distribution/reference v0.6.0 // indirect
	github.com/docker/go-connections v0.8.1 // indirect
	github.com/docker/go-units v0.5.0 // indirect
	github.com/felixge/httpsnoop v1.1.0 // indirect
	github.com/go-logr/stdr v1.2.2 // indirect
	github.com/goccy/go-yaml v1.19.2 // indirect
	github.com/google/dotprompt/go v0.0.0-20260708220100-73beb993ac95 // indirect
	github.com/google/uuid v1.6.0 // indirect
	github.com/k8snetworkplumbingwg/network-attachment-definition-client v0.0.0-20191119172530-79f836b90111 // indirect
	github.com/klauspost/compress v1.20.0 // indirect
	github.com/mbleigh/raymond v0.0.0-20250414171441-6b3a58ab9e0a // indirect
	github.com/moby/docker-image-spec v1.3.1 // indirect
	github.com/moby/go-archive v0.3.3 // indirect
	github.com/moby/moby/api v1.55.0 // indirect
	github.com/moby/patternmatcher v0.6.1 // indirect
	github.com/moby/sys/sequential v0.7.0 // indirect
	github.com/moby/sys/user v0.4.1 // indirect
	github.com/moby/sys/userns v0.1.0 // indirect
	github.com/moby/term v0.5.2 // indirect
	github.com/openai/openai-go v1.8.2 // indirect
	github.com/opencontainers/go-digest v1.0.0 // indirect
	github.com/opencontainers/image-spec v1.1.1 // indirect
	github.com/openshift/client-go v0.0.0-20210112165513-ebc401615f47 // indirect
	github.com/pb33f/ordered-map/v2 v2.3.1 // indirect
	github.com/pkg/errors v0.9.1 // indirect
	github.com/sirupsen/logrus v1.10.2 // indirect
	github.com/tidwall/gjson v1.19.0 // indirect
	github.com/tidwall/match v1.1.1 // indirect
	github.com/tidwall/pretty v1.2.1 // indirect
	github.com/tidwall/sjson v1.2.5 // indirect
	github.com/xeipuuv/gojsonpointer v0.0.0-20190905194746-02993c407bfb // indirect
	github.com/xeipuuv/gojsonreference v0.0.0-20180127040603-bd5ef7bd5415 // indirect
	github.com/xeipuuv/gojsonschema v1.2.0 // indirect
	github.com/yosida95/uritemplate/v3 v3.0.2 // indirect
	go.opentelemetry.io/auto/sdk v1.2.1 // indirect
	go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp v0.71.0 // indirect
	go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp v1.46.0
	go.opentelemetry.io/otel/exporters/stdout/stdouttrace v1.46.0
	go.yaml.in/yaml/v4 v4.0.0-rc.4 // indirect
	golang.org/x/sys v0.48.0
)

require (
	dario.cat/mergo v1.0.2 // indirect
	github.com/ProtonMail/go-crypto v1.4.1 // indirect
	github.com/TcM1911/r2g2 v0.3.2 // indirect
	github.com/beorn7/perks v1.0.1 // indirect
	github.com/blacktop/go-dwarf v1.0.14 // indirect
	github.com/blacktop/go-macho v1.1.271 // indirect
	github.com/bluekeyes/go-gitdiff v0.8.1 // indirect
	github.com/cheynewallace/tabby v1.1.1 // indirect
	github.com/cloudflare/circl v1.6.3 // indirect
	github.com/cyphar/filepath-securejoin v0.6.1 // indirect
	github.com/dustin/go-humanize v1.0.1 // indirect
	github.com/elliotchance/orderedmap v1.4.0 // indirect
	github.com/emirpasic/gods v1.18.1 // indirect
	github.com/fxamacker/cbor/v2 v2.9.1 // indirect
	github.com/gertd/go-pluralize v0.2.1 // indirect
	github.com/go-git/gcfg v1.5.1-0.20230307220236-3a3c6141e376 // indirect
	github.com/go-git/go-billy/v5 v5.8.0 // indirect
	github.com/go-git/go-git/v5 v5.17.0 // indirect
	github.com/gogo/protobuf v1.3.2 // indirect
	github.com/golang-jwt/jwt/v4 v4.5.2 // indirect
	github.com/golang/groupcache v0.0.0-20241129210726-2c02b8208cf8 // indirect
	github.com/goretk/gore v0.13.27 // indirect
	github.com/goretk/redress v1.2.64 // indirect
	github.com/inconshreveable/mousetrap v1.1.0 // indirect
	github.com/jbenet/go-context v0.0.0-20150711004518-d14ea06fba99 // indirect
	github.com/json-iterator/go v1.1.12 // indirect
	github.com/kevinburke/ssh_config v1.4.0 // indirect
	github.com/klauspost/cpuid/v2 v2.4.0 // indirect
	github.com/mandiant/GoReSym v1.7.1 // indirect
	github.com/modern-go/concurrent v0.0.0-20180306012644-bacd9c7ef1dd // indirect
	github.com/modern-go/reflect2 v1.0.3-0.20250322232337-35a7c28c31ee // indirect
	github.com/munnerz/goautoneg v0.0.0-20191010083416-a7dc8b61c822 // indirect
	github.com/pjbgf/sha1cd v0.5.0 // indirect
	github.com/prometheus/client_model v0.6.2 // indirect
	github.com/rogpeppe/go-internal v1.14.1 // indirect
	github.com/sergi/go-diff v1.4.0 // indirect
	github.com/skeema/knownhosts v1.3.2 // indirect
	github.com/spf13/cobra v1.10.2 // indirect
	github.com/spf13/pflag v1.0.10 // indirect
	github.com/x448/float16 v0.8.4 // indirect
	github.com/xanzy/ssh-agent v0.3.3 // indirect
	go.yaml.in/yaml/v2 v2.4.4 // indirect
	golang.org/x/arch v0.29.0 // indirect
	golang.org/x/crypto v0.57.0 // indirect
	golang.org/x/mod v0.41.0 // indirect
	golang.org/x/net v0.59.0
	golang.org/x/tools v0.51.0 // indirect
	google.golang.org/protobuf v1.36.12 // indirect
	gopkg.in/inf.v0 v0.9.1 // indirect
	gopkg.in/warnings.v0 v0.1.2 // indirect
	k8s.io/component-base v0.37.0 // indirect
	k8s.io/gengo/v2 v2.0.0-20260408192533-25e2208e0dc3 // indirect
	k8s.io/klog/v2 v2.140.0 // indirect
	k8s.io/kube-openapi v0.0.0-20260721132016-d427ff9ee9ad // indirect
	k8s.io/utils v0.0.0-20260626114624-be93311217bd // indirect
	mvdan.cc/garble v0.16.0 // indirect
	// controller-gen is a build-time tool for the controller deepcopy actions.
	sigs.k8s.io/controller-tools v0.20.0 // indirect
	sigs.k8s.io/json v0.0.0-20250730193827-2d320260d730 // indirect
	sigs.k8s.io/randfill v1.0.0 // indirect
	sigs.k8s.io/structured-merge-diff/v6 v6.4.2 // indirect
	sigs.k8s.io/yaml v1.6.0 // indirect
)

require (
	github.com/mark3labs/mcp-go v1.0.0
	github.com/prometheus/client_golang v1.24.1
	github.com/prometheus/common v0.71.0
	github.com/prometheus/procfs v0.22.0
	go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc v0.70.0
	go.opentelemetry.io/otel v1.46.0
	go.opentelemetry.io/otel/exporters/otlp/otlptrace v1.46.0
	go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc v1.44.0
	go.opentelemetry.io/otel/metric v1.46.0
	go.opentelemetry.io/otel/sdk v1.46.0
	go.opentelemetry.io/otel/trace v1.46.0
	go.opentelemetry.io/proto/otlp v1.11.0 // indirect
	golang.org/x/sync v0.24.0
	golang.org/x/text v0.43.0
	google.golang.org/genproto/googleapis/api v0.0.0-20260911204522-f61a6ca850bd
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260911204522-f61a6ca850bd
	google.golang.org/grpc v1.83.2
)

require (
	github.com/davecgh/go-spew v1.1.2-0.20180830191138-d8f796af33cc // indirect
	github.com/emicklei/go-restful/v3 v3.13.0 // indirect
	github.com/evanphx/json-patch/v5 v5.9.11 // indirect
	github.com/fatih/color v1.18.0 // indirect
	github.com/fsnotify/fsnotify v1.9.0 // indirect
	github.com/go-logr/zapr v1.3.0 // indirect
	github.com/go-openapi/jsonpointer v1.0.0 // indirect
	github.com/go-openapi/jsonreference v1.0.0 // indirect
	github.com/go-openapi/swag v0.28.0 // indirect
	github.com/go-openapi/swag/cmdutils v0.28.0 // indirect
	github.com/go-openapi/swag/conv v0.28.0 // indirect
	github.com/go-openapi/swag/fileutils v0.28.0 // indirect
	github.com/go-openapi/swag/jsonutils v0.28.0 // indirect
	github.com/go-openapi/swag/loading v0.28.0 // indirect
	github.com/go-openapi/swag/mangling v0.28.0 // indirect
	github.com/go-openapi/swag/netutils v0.28.0 // indirect
	github.com/go-openapi/swag/pools v0.28.0 // indirect
	github.com/go-openapi/swag/stringutils v0.28.0 // indirect
	github.com/go-openapi/swag/typeutils v0.28.0 // indirect
	github.com/go-openapi/swag/yamlutils v0.28.0 // indirect
	github.com/gobuffalo/flect v1.0.3 // indirect
	github.com/google/gnostic-models v0.7.0 // indirect
	github.com/google/go-querystring v1.2.0 // indirect
	github.com/josharian/intern v1.0.0 // indirect
	github.com/lib/pq v1.10.9
	github.com/mailru/easyjson v0.9.0 // indirect
	github.com/mattn/go-colorable v0.1.15 // indirect
	github.com/mattn/go-isatty v0.0.24 // indirect
	github.com/mfridman/interpolate v0.0.2
	github.com/openshift/custom-resource-status v1.1.2 // indirect
	github.com/pmezard/go-difflib v1.0.1-0.20181226105442-5d4384ee4fb2 // indirect
	github.com/pressly/goose/v3 v3.28.0
	github.com/sethvargo/go-retry v0.4.0
	github.com/siderolabs/go-pointer v1.0.1 // indirect
	github.com/siderolabs/protoenc v0.2.4 // indirect
	go.uber.org/multierr v1.11.0 // indirect
	go.yaml.in/yaml/v3 v3.0.5 // indirect
	golang.org/x/term v0.46.0 // indirect
	golang.org/x/time v0.15.0 // indirect
	gomodules.xyz/jsonpatch/v2 v2.4.0 // indirect
	gopkg.in/evanphx/json-patch.v4 v4.13.0 // indirect
	k8s.io/apiextensions-apiserver v0.37.0 // indirect
	k8s.io/code-generator v0.37.0 // indirect
	kubevirt.io/containerized-data-importer-api v1.64.0 // indirect
	kubevirt.io/controller-lifecycle-operator-sdk/api v0.2.4 // indirect
)

require (
	github.com/cenkalti/backoff/v4 v4.3.0 // indirect
	github.com/containerd/platforms v0.2.1 // indirect
	github.com/cpuguy83/dockercfg v0.3.2 // indirect
	github.com/ebitengine/purego v0.10.1 // indirect
	github.com/go-ole/go-ole v1.3.0 // indirect
	github.com/lufia/plan9stats v0.0.0-20260330125221-c963978e514e // indirect
	github.com/magiconair/properties v1.8.10 // indirect
	github.com/ncruces/go-strftime v1.0.0 // indirect
	github.com/pelletier/go-toml/v2 v2.4.3 // indirect
	github.com/power-devops/perfstat v0.0.0-20240221224432-82ca36839d55 // indirect
	github.com/remyoudompheng/bigfft v0.0.0-20230129092748-24d4a6f8daec // indirect
	github.com/sagikazarmark/locafero v0.11.0 // indirect
	github.com/shirou/gopsutil/v4 v4.26.6 // indirect
	github.com/sourcegraph/conc v0.3.1-0.20240121214520-5f936abd7ae8 // indirect
	github.com/spf13/afero v1.15.0 // indirect
	github.com/spf13/cast v1.10.0 // indirect
	github.com/stretchr/testify v1.12.1 // indirect
	github.com/subosito/gotenv v1.6.0 // indirect
	github.com/tklauser/go-sysconf v0.4.0 // indirect
	github.com/tklauser/numcpus v0.12.0 // indirect
	github.com/yusufpapurcu/wmi v1.2.4 // indirect
	modernc.org/libc v1.75.6 // indirect
	modernc.org/mathutil v1.7.1 // indirect
	modernc.org/memory v1.12.1 // indirect
	modernc.org/sqlite v1.57.0
)

require (
	github.com/bmatcuk/doublestar/v4 v4.10.3 // indirect
	github.com/cilium/ebpf v0.16.0 // indirect
	github.com/coreos/go-systemd/v22 v22.7.0
	github.com/go-chi/chi/v5 v5.3.2 // indirect
	github.com/godbus/dbus/v5 v5.1.0
	github.com/google/jsonschema-go v0.4.3 // indirect
	github.com/gorilla/securecookie v1.1.2
	github.com/muhlemmer/gu v0.3.1 // indirect
	github.com/muhlemmer/httpforwarded v0.1.0 // indirect
	github.com/opencontainers/runtime-spec v1.3.0 // indirect
	github.com/rs/cors v1.11.1 // indirect
	github.com/segmentio/asm v1.2.1 // indirect
	github.com/segmentio/encoding v0.5.4 // indirect
	github.com/zitadel/schema v1.3.2 // indirect
	golang.org/x/exp v0.0.0-20260824195058-e88cd73687aa // indirect
)

// Indirect dependencies of client-go's tools/remotecommand, which the Action Service's sandbox Actions exec into a box
// through
require (
	github.com/gorilla/websocket v1.5.4-0.20250319132907-e064f32e3674 // indirect
	github.com/moby/spdystream v0.5.1 // indirect
	go.uber.org/mock v0.5.1
	k8s.io/streaming v0.37.0 // indirect
	kubevirt.io/client-go v1.8.2
)
