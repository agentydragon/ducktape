# Persistent AI Agents Platform Plan

## Vision

Deploy long-running AI agents with their own persistent computing resources (storage, credentials, isolated
environments) where agents can execute arbitrary long-running tasks with full computer control capabilities.

## Architecture Overview

```text
┌─────────────────────────────────────────────────────────┐
│ Chat UI (Kagent or LibreChat)                           │
└────────────────────┬────────────────────────────────────┘
                     │ User chats with agent
                     ▼
┌─────────────────────────────────────────────────────────┐
│ Kagent Agent CRD                                        │
│  - System prompt                                        │
│  - References: devbot-computer-control (RemoteMCPServer)│
└────────────────────┬────────────────────────────────────┘
                     │ HTTP calls to MCP server
                     ▼
┌────────────────────────────────────────────────────────┐
│ Per-Agent Pod: devbot-desktop                          │
│                                                        │
│  ┌──────────────────────────────────────────────────┐  │
│  │ MCP Server (Sidecar Container)                   │  │
│  │  - Port 8080                                     │  │
│  │  - DISPLAY=localhost:0                           │  │
│  └────────────┬─────────────────────────────────────┘  │
│               │ X11 (localhost)                        │
│               ▼                                        │
│  ┌──────────────────────────────────────────────────┐  │
│  │ Desktop Container                                │  │
│  │  - Ubuntu + Xfce                                 │  │
│  │  - X11 server :0                                 │  │
│  │  - VNC :5900                                     │  │
│  │  - Workspace PVC                                 │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────┘

Resources created per agent:
1. Agent CRD (defines agent behavior)
2. RemoteMCPServer CRD (points to desktop service)
3. Deployment (desktop + MCP sidecar)
4. Service (exposes VNC + MCP ports)
5. PVCs (workspace + config)
6. ExternalSecret (credentials from Vault)
```

## Component Breakdown

### 1. Chat Interface

**Options:**

- **Kagent UI** (built-in, per-agent chat with sessions)
- **LibreChat** (more features, OIDC support, multi-model)
- **Open WebUI** (lightweight alternative)

**Recommendation:** Start with Kagent UI (comes with the platform)

### 2. Agent Orchestration (Kagent)

**Kagent Agent CRD:**

```yaml
apiVersion: kagent.dev/v1alpha2
kind: Agent
metadata:
  name: devbot
  namespace: agents
spec:
  type: Declarative
  description: "Development assistant with full computer control"

  declarative:
    systemMessage: |
      You are DevBot, a development assistant with access to your own Linux desktop.
      You can use these capabilities:
      - Take screenshots to see the current state
      - Click on UI elements
      - Type text and press keys
      - Execute shell commands
      - Clone repos, run tests, debug issues

      Your workspace is persistent at /home/agent/workspace.
      You have credentials pre-configured for:
      - Gitea: git.allegedly.works
      - Harbor: registry.allegedly.works

    modelConfig: anthropic-claude

    tools:
      - type: McpServer
        mcpServer:
          name: devbot-computer-control
          kind: RemoteMCPServer
          apiGroup: kagent.dev
          # Note: toolNames can be omitted to use all available tools
          toolNames:
            - take_screenshot
            - click_screen
            - move_mouse
            - type_text
            - press_key
            - get_screen_size

    deployment:
      replicas: 1
      env:
        - name: ANTHROPIC_API_KEY
          valueFrom:
            secretKeyRef:
              name: devbot-secrets
              key: anthropic-key
      resources:
        requests:
          cpu: "500m"
          memory: "1Gi"
```

**Key Features:**

- Agents defined as K8s CRDs
- Tools via MCP protocol only (no other options)
- Can reference other agents as tools
- Deployment spec embedded (volumes, env, resources)

### 3. MCP Server (computer-control-mcp)

**Selected:** `computer-control-mcp` by AB498

**Why:**

- ✅ Most complete tool set (mouse, keyboard, screen, OCR, window management)
- ✅ Python-based (easy to containerize)
- ✅ No size limitations (unlike tanob's 1MB screenshot limit)
- ✅ Docker support included
- ✅ MIT licensed
- ✅ Cross-platform (Linux/Windows/macOS)

**Repository:** <https://github.com/AB498/computer-control-mcp>

#### Architecture: 2 Containers Pattern

```text
┌─────────────────────────────────┐
│ MCP Server Container            │  Runs computer-control-mcp
│  - Python 3.12 + dependencies   │  Exposes MCP tools over SSE
│  - computer-control-mcp code    │  Connects to Desktop via X11
│  - Connects to DISPLAY=:0       │
└────────────┬────────────────────┘
             │ X11 protocol (localhost network)
             ▼
┌─────────────────────────────────┐
│ Desktop Container               │  Runs the actual desktop
│  - Ubuntu + Xfce + VNC          │  Agent's workspace & applications
│  - X11 server (DISPLAY=:0)      │  What the agent "sees" and controls
│  - VNC server (port 5900)       │  Optional human monitoring
│  - Workspace PVC mounted        │
└─────────────────────────────────┘
```

**Why 2 containers?**

- **Separation of concerns:** Desktop environment vs control interface
- **Independent scaling:** Can restart MCP server without killing desktop
- **Easier updates:** Update computer-control-mcp without rebuilding desktop image
- **Debugging:** Can connect to desktop via VNC while MCP server runs

**How they communicate:**

- Desktop container runs X11 server on DISPLAY=:0
- MCP container sets DISPLAY=localhost:0 to connect via X11 protocol
- Both in same pod → share localhost network → low latency

**Transport Configuration:**

- **Kagent supports:** SSE and STREAMABLE_HTTP
- **FastMCP (computer-control-mcp) supports:** stdio, SSE, HTTP
- **Selected:** STREAMABLE_HTTP (Kagent's default)
- **Important:** Default `computer-control-mcp` CLI uses stdio which doesn't work over network!
- **Solution:** Explicitly run with `mcp.run(transport='http')` in container

**Tools Provided:**

- `screenshot` - Capture screen
- `mouse_click(x, y, button, clicks)` - Click at coordinates
- `mouse_move(x, y)` - Move cursor
- `mouse_down(button)` / `mouse_up(button)` - Hold/release
- `drag_mouse(x1, y1, x2, y2)` - Drag operation
- `type_text(text)` - Type at cursor
- `press_key(key)` - Press individual keys
- `key_down(key)` / `key_up(key)` - Hold/release keys
- `ocr_screenshot()` - Extract text with coordinates
- `get_screen_size()` - Display resolution
- Window management tools

**Container Image (`computer-control-mcp:latest`):**

```dockerfile
FROM python:3.12-slim

# Install X11 client libraries (NOT the X server itself)
RUN apt-get update && apt-get install -y \
    libx11-6 libxext6 libxrandr2 libxfixes3 \
    libxinerama1 libxcursor1 libxtst6 \
    gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Clone and install computer-control-mcp
WORKDIR /app
RUN pip install --no-cache-dir \
    pyautogui==0.9.54 \
    mcp[cli]==1.13.0 \
    pillow==11.3.0 \
    opencv-python==4.12.0.88 \
    rapidocr==3.3.1 \
    onnxruntime==1.22.0 \
    mss>=7.0.0 \
    numpy \
    pygetwindow \
    fuzzywuzzy \
    python-Levenshtein

# Copy computer-control-mcp code
COPY computer-control-mcp/ /app/

# Install package
RUN pip install -e .

# Expose HTTP port
EXPOSE 8080

# Run MCP server with STREAMABLE_HTTP transport
# Note: Default "computer-control-mcp" uses stdio which doesn't work over network
CMD ["python", "-c", "from computer_control_mcp.core import mcp; mcp.run(transport='http', host='0.0.0.0', port=8080)"]
```

**Build:**

```bash
cd /code/github.com/AB498/computer-control-mcp
docker build -t computer-control-mcp:latest -f Dockerfile.mcp .
```

**RemoteMCPServer CRD (per-agent):**

```yaml
apiVersion: kagent.dev/v1alpha2
kind: RemoteMCPServer
metadata:
  name: devbot-computer-control
  namespace: agents
spec:
  description: "Computer control MCP server for devbot agent (private instance)"
  protocol: STREAMABLE_HTTP
  url: "http://devbot-desktop.agents.svc.cluster.local:8080" # Points to desktop service
  timeout: 30s
```

**Note:** Each agent gets its own RemoteMCPServer CRD pointing to that agent's desktop service.
The MCP server runs as a sidecar in the desktop pod (see section 4 below).

### 4. Agent's Private Desktop

**Current Plan:** Simple Pod with PVC (not StatefulSet) + MCP Server sidecar

**⚠️ Known Limitation:**

- Pod restarts = potential data loss for in-memory state
- Workspace files persist (PVC), but running processes don't
- **Trade-off:** Simpler to start with, can upgrade to StatefulSet later

**Architecture:** Both containers in same pod sharing localhost network

```text
┌────────────────────────────────────────────────────────┐
│ Pod: devbot-desktop                                    │
│                                                        │
│  ┌────────────────────┐  ┌──────────────────────────┐  │
│  │ MCP Server         │  │ Desktop                  │  │
│  │                    │  │                          │  │
│  │ Port 8080          │  │ X11 Server :0            │  │
│  │ DISPLAY=localhost:0│←→│ VNC Server 5900          │  │
│  └────────────────────┘  │ /workspace → PVC         │  │
│                          └──────────────────────────┘  │
└────────────────────────────────────────────────────────┘
```

**Deployment:**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: devbot-desktop
  namespace: agents
spec:
  replicas: 1
  selector:
    matchLabels:
      app: devbot-desktop
      agent: devbot
  template:
    metadata:
      labels:
        app: devbot-desktop
        agent: devbot
    spec:
      initContainers:
        - name: setup-credentials
          image: busybox
          command: ["/bin/sh", "-c"]
          args:
            - |
              # Setup git credentials
              mkdir -p /workspace/.config/git
              cat > /workspace/.config/git/config <<EOF
              [user]
                name = DevBot
                email = devbot@agents.local
              [credential]
                helper = store
              EOF

              cat > /workspace/.config/git/credentials <<EOF
              https://devbot:${GITEA_TOKEN}@git.allegedly.works
              EOF
              chmod 600 /workspace/.config/git/credentials
          env:
            - name: GITEA_TOKEN
              valueFrom:
                secretKeyRef:
                  name: devbot-credentials
                  key: gitea-token
          volumeMounts:
            - name: workspace
              mountPath: /workspace

      containers:
        # Desktop container with X11 + VNC
        - name: desktop
          image: devbot-desktop:latest
          env:
            - name: VNC_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: devbot-credentials
                  key: vnc-password
            - name: DISPLAY
              value: ":0"
          ports:
            - name: vnc
              containerPort: 5900
            - name: x11
              containerPort: 6000
          volumeMounts:
            - name: workspace
              mountPath: /home/agent/workspace
            - name: config
              mountPath: /home/agent/.config
          resources:
            requests:
              cpu: "1"
              memory: "2Gi"
            limits:
              cpu: "2"
              memory: "4Gi"

        # MCP server sidecar
        - name: mcp-server
          image: computer-control-mcp:latest
          env:
            - name: DISPLAY
              value: "localhost:0" # Connect to desktop container
          ports:
            - name: mcp
              containerPort: 8080
          resources:
            requests:
              cpu: "250m"
              memory: "512Mi"

      volumes:
        - name: workspace
          persistentVolumeClaim:
            claimName: devbot-workspace
        - name: config
          persistentVolumeClaim:
            claimName: devbot-config
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: devbot-workspace
  namespace: agents
spec:
  storageClassName: proxmox-csi
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 20Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: devbot-config
  namespace: agents
spec:
  storageClassName: proxmox-csi
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 5Gi
---
apiVersion: v1
kind: Service
metadata:
  name: devbot-desktop
  namespace: agents
spec:
  selector:
    app: devbot-desktop
    agent: devbot
  ports:
    - name: vnc
      port: 5900
      targetPort: 5900
    - name: mcp
      port: 8080
      targetPort: 8080
```

**Key points:**

- MCP server is a **sidecar** in the desktop pod, not a separate deployment
- Each agent has its own private MCP server instance
- Service exposes both VNC (5900) and MCP (8080) from the same pod
- RemoteMCPServer CRD points to `http://devbot-desktop:8080` (not a separate service)

**Desktop Image (`devbot-desktop:latest`):**

```dockerfile
FROM ubuntu:22.04

# Install desktop environment
RUN apt-get update && apt-get install -y \
    xfce4 xfce4-terminal \
    tigervnc-standalone-server \
    dbus-x11 \
    firefox \
    git curl wget \
    python3 python3-pip \
    nodejs npm \
    build-essential \
    vim nano \
    # X11 libraries required by computer-control-mcp
    libx11-6 libxext6 libxrandr2 libxfixes3 \
    libxinerama1 libxcursor1 libxtst6 \
    && rm -rf /var/lib/apt/lists/*

# Create agent user
RUN useradd -m -s /bin/bash agent && \
    echo "agent ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# VNC setup
USER agent
WORKDIR /home/agent

# VNC password and xstartup
RUN mkdir -p ~/.vnc && \
    echo '#!/bin/bash\nstartxfce4 &' > ~/.vnc/xstartup && \
    chmod +x ~/.vnc/xstartup

# Startup script
COPY --chown=agent:agent start-desktop.sh /home/agent/
RUN chmod +x /home/agent/start-desktop.sh

EXPOSE 5900 6000

CMD ["/home/agent/start-desktop.sh"]
```

**start-desktop.sh:**

```bash
#!/bin/bash

# Set VNC password from env
echo "${VNC_PASSWORD:-password}" | vncpasswd -f > ~/.vnc/passwd
chmod 600 ~/.vnc/passwd

# Start VNC server
vncserver :0 -geometry 1920x1080 -depth 24 -localhost no

# Keep container running
tail -f ~/.vnc/*.log
```

### 5. Credentials Management (ESO)

**ExternalSecret for per-agent credentials:**

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: devbot-credentials
  namespace: agents
spec:
  refreshInterval: 8760h # 1 year (avoid rotation issues)
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: devbot-credentials
  data:
    - secretKey: anthropic-key
      remoteRef:
        key: secret/agents/devbot
        property: anthropic-key
    - secretKey: gitea-token
      remoteRef:
        key: secret/agents/devbot
        property: gitea-token
    - secretKey: harbor-password
      remoteRef:
        key: secret/agents/devbot
        property: harbor-password
    - secretKey: vnc-password
      remoteRef:
        key: secret/agents/devbot
        property: vnc-password
```

## CLI-Only Prototype (No Visual Capabilities)

**Simpler First Step:** Start without desktop/VNC, just shell access

**Benefits:**

- No X11/VNC complexity
- Faster to prototype
- Still fully functional for CLI tasks
- Can add visual capabilities later

**Architecture:**

```text
Kagent Agent → MCP Server (shell tools) → Agent Container (bash shell)
```

**MCP Server for CLI:**

- Use simple exec-based MCP server
- Tools: `run_command`, `read_file`, `write_file`, `list_directory`
- No desktop environment needed

**Agent Container (Simplified):**

```dockerfile
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y \
    git curl wget \
    python3 python3-pip \
    nodejs npm \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Agent user
RUN useradd -m -s /bin/bash agent
USER agent
WORKDIR /home/agent

CMD ["sleep", "infinity"]
```

**Upgrade Path:**

1. **Phase 1:** CLI-only agent (prove persistence, credentials, MCP integration)
2. **Phase 2:** Add desktop + computer-control-mcp (full visual capabilities)

## Helm Chart Structure

**Per-Agent Helm Chart:**

```text
charts/kagent-agent/
├── Chart.yaml
├── values.yaml
├── templates/
│   ├── agent.yaml              # Kagent Agent CRD
│   ├── mcp-deployment.yaml     # MCP server
│   ├── mcp-service.yaml        # MCP server service
│   ├── desktop-deployment.yaml # Agent's desktop/shell
│   ├── desktop-service.yaml    # Desktop services
│   ├── pvcs.yaml               # Persistent volumes
│   ├── externalsecret.yaml     # ESO credentials
│   └── _helpers.tpl            # Template helpers
```

**values.yaml:**

```yaml
agent:
  name: devbot
  namespace: agents
  systemMessage: "You are DevBot..."
  modelConfig: anthropic-claude

  # Visual capabilities
  desktop:
    enabled: true # false for CLI-only
    image: devbot-desktop:latest
    storage:
      workspace: 20Gi
      config: 5Gi

  # MCP server
  mcp:
    image: computer-control-mcp:latest # or custom CLI MCP server
    type: visual # or 'cli'

  # Credentials from Vault
  credentials:
    vault:
      path: secret/agents/devbot
    gitea:
      enabled: true
    harbor:
      enabled: true

resources:
  agent:
    requests:
      cpu: "500m"
      memory: "1Gi"
  desktop:
    requests:
      cpu: "1"
      memory: "2Gi"
    limits:
      cpu: "2"
      memory: "4Gi"
  mcp:
    requests:
      cpu: "250m"
      memory: "512Mi"
```

**Deploy multiple agents:**

```bash
# Agent 1: DevBot with desktop
helm install devbot ./charts/kagent-agent \
  --set agent.name=devbot \
  --set agent.systemMessage="You are a development assistant..." \
  --set agent.desktop.enabled=true

# Agent 2: DataBot CLI-only
helm install databot ./charts/kagent-agent \
  --set agent.name=databot \
  --set agent.systemMessage="You are a data analysis assistant..." \
  --set agent.desktop.enabled=false \
  --set agent.mcp.type=cli
```

## Implementation Phases

### Phase 0: Research & Planning ✅ Complete

Architecture designed, `computer-control-mcp` verified locally on Pop!\_OS (all tools:
screenshot, mouse, keyboard, OCR). K8s manifests and Dockerfiles created in
`k8s/agents/devbot/`. Kagent platform reviewed.

### Phase 1: CLI-Only Prototype

- [ ] Deploy Kagent on cluster (unsuspend kustomizations first)
- [ ] Create simple exec-based MCP server
- [ ] Build agent container (CLI, no desktop)
- [ ] Deploy single agent with persistent storage
- [ ] Test: Agent clones repo, runs commands, persists workspace
- [ ] Validate credentials injection works
- [ ] Test agent restart (verify workspace persistence)

### Phase 2: Add Visual Capabilities

**Note:** Kagent kustomizations (`kagent`, `kagent-namespace`, `kagent-secrets`) are
currently **suspended** in Flux. Kagent is not actively deployed. Resume Phase 2 by
unsuspending these kustomizations.

- [ ] Unsuspend Kagent kustomizations and verify platform is running
- [ ] Test Kagent UI at <https://kagent.allegedly.works>
- [ ] Load devbot container images into cluster nodes (`talosctl image import`)
- [ ] Store secrets in Vault (`kv/agents/devbot`)
- [ ] Deploy agent desktop pod (desktop + MCP sidecar)
- [ ] Test basic pod startup and health
- [ ] Verify X11 connectivity between MCP server and desktop containers
- [ ] Test VNC access to desktop
- [ ] Verify MCP server responds on port 8080
- [ ] Deploy RemoteMCPServer CRD
- [ ] Deploy Kagent Agent CRD
- [ ] Test end-to-end: Chat → Agent → MCP → Desktop actions
- [ ] Validate persistent storage (workspace survives pod restart)
- [ ] Test credentials injection (Gitea token, Harbor password)

## Current Status: Ready for build and deployment

**Files created:**

- `/home/agentydragon/code/cluster/k8s/agents/devbot/`
  - `namespace.yaml` - agents namespace
  - `pvcs.yaml` - 20Gi workspace + 5Gi config storage
  - `externalsecret.yaml` - Vault credentials integration
  - `deployment.yaml` - Desktop + MCP sidecar containers
  - `service.yaml` - VNC:5900 + MCP:8080 endpoints
  - `remotemcpserver.yaml` - RemoteMCPServer CRD for Kagent
  - `agent.yaml` - Agent CRD with system prompt
  - `kustomization.yaml` - Resource aggregation
- `/home/agentydragon/code/cluster/k8s/agents/devbot/docker/`
  - `desktop/Dockerfile` - Ubuntu + Xfce + VNC + X11
  - `desktop/xstartup` - VNC startup script
  - `desktop/entrypoint.sh` - VNC password and server initialization
  - `mcp-server/Dockerfile` - Python 3.12 + computer-control-mcp with STREAMABLE_HTTP
  - `build.sh` - Build script (defaults to local, optionally pushes to Harbor)

**Next Steps:**

1. **Build images:**

   ```bash
   cd /home/agentydragon/code/cluster/k8s/agents/devbot/docker
   ./build.sh
   ```

2. **Store secrets in Vault:**

   ```bash
   # Generate secrets
   ANTHROPIC_KEY="sk-ant-..."  # Your Anthropic API key
   GITEA_TOKEN=$(openssl rand -base64 32)
   HARBOR_PASSWORD=$(openssl rand -base64 32)
   VNC_PASSWORD=$(openssl rand -base64 16)

   # Store in Vault
   vault kv put secret/agents/devbot \
     anthropic-key="$ANTHROPIC_KEY" \
     gitea-token="$GITEA_TOKEN" \
     harbor-password="$HARBOR_PASSWORD" \
     vnc-password="$VNC_PASSWORD"
   ```

3. **Deploy to cluster:**

   ```bash
   kubectl apply -k /home/agentydragon/code/cluster/k8s/agents/devbot/
   ```

4. **Check deployment status:**

   ```bash
   kubectl get pods -n agents -w
   kubectl logs -n agents deployment/devbot-desktop -c desktop
   kubectl logs -n agents deployment/devbot-desktop -c mcp-server
   ```

5. **Test VNC access:**

   ```bash
   kubectl port-forward -n agents service/devbot-desktop 5900:5900
   # Connect with VNC client to localhost:5900
   ```

6. **Test MCP server:**

   ```bash
   kubectl port-forward -n agents service/devbot-desktop 8080:8080
   curl http://localhost:8080/health
   ```

7. **Verify RemoteMCPServer:**

   ```bash
   kubectl get remotemcpserver -n agents
   kubectl describe remotemcpserver devbot-computer-control -n agents
   ```

8. **Deploy Kagent (if not already deployed):**
   - Follow Kagent installation docs
   - Ensure ClusterSecretStore for Vault is configured

9. **Test Agent CRD:**

   ```bash
   kubectl get agent -n agents
   kubectl describe agent devbot -n agents
   # Check agent controller logs for connection status
   ```

10. **Test end-to-end workflow:**
    - Access Kagent UI
    - Start chat with devbot agent
    - Request screenshot: "Take a screenshot of the desktop"
    - Request mouse action: "Click at coordinates 500, 300"
    - Verify actions appear in desktop via VNC

### Phase 3: Multi-Agent Support

- [ ] Create Helm chart template
- [ ] Deploy 2-3 agents with different purposes
- [ ] Test agent isolation (network policies)
- [ ] Validate per-agent credentials work
- [ ] Test concurrent agent operations

### Phase 4: Production Hardening

- [ ] Add monitoring (Prometheus metrics)
- [ ] Add logging (Loki integration)
- [ ] Network policies (agent isolation)
- [ ] Resource quotas per agent
- [ ] Backup strategy for agent workspaces
- [ ] Document operational procedures

## Future Enhancements (Bookmarked)

### Multi-Agent Orchestration (CrewAI)

**When:** After single-agent architecture is proven
**Why:** Complex tasks requiring collaboration between specialized agents
**What:** Kagent supports CrewAI integration for multi-agent workflows
**Reference:** <https://github.com/kagent-dev/kagent/tree/main/python/packages/kagent-crewai>
**Features:**

- Agent crews with defined roles (researcher, writer, analyst)
- Task delegation between agents
- Hierarchical agent structures
- Shared context and memory
  **Use Cases:**
- Research crew (searcher + analyzer + writer)
- DevOps crew (planner + implementer + tester)
- Complex workflows requiring specialized expertise per step

### OpenTelemetry Integration

**When:** After agents are deployed in production
**Why:** Trace agent tool calls, observe decision paths, debug failures
**What:** Add OpenTelemetry to cluster + Kagent agent traces
**Benefits:**

- Trace agent → LLM → tool call chains
- Visualize agent reasoning paths
- Identify bottlenecks (slow tools, LLM latency)
- Debug multi-agent coordination
- Performance optimization data
  **Implementation:**
- Deploy OpenTelemetry Collector in cluster
- Configure Kagent agents with OTEL exporters
- Jaeger or Tempo for trace storage
- Grafana for visualization
  **Integration:** Ties into cluster observability stack (Prometheus, Loki, Grafana)
  **Reference:** docs/plan.md observability section

### Agent Sandbox Integration

**When:** After basic architecture is stable
**Why:** Stronger isolation, pre-warmed pools, sub-second cold starts
**What:** Replace Deployment with Agent Sandbox CRD
**Reference:** kubernetes-sigs/agent-sandbox

### StatefulSet for Desktop

**When:** If pod restart data loss becomes problematic
**Why:** Stable pod identity, ordered deployment
**What:** Change desktop Deployment → StatefulSet
**Trade-off:** More complexity, slower rollouts

### Proxmox VM Backend

**When:** If K8s pods prove insufficient for isolation
**Why:** Full VM isolation, traditional desktop environment
**What:** Terraform-managed VMs, MCP server in K8s connects to VMs
**Trade-off:** Higher resource usage, slower provisioning

### Guacamole Integration

**When:** Need web-based monitoring/debugging of agent desktops
**Why:** View agent desktop in browser without VNC client
**What:** Deploy Guacamole + Authentik RAC
**Reference:** docs/plan.md Guacamole section

## Known Limitations & Trade-offs

### Current Architecture

#### Desktop Pod Restart = Process Loss

- Workspace files persist (PVC)
- Running processes do NOT persist
- In-memory state lost
- **Impact:** Long-running compilations, downloads interrupted
- **Mitigation:** Agent can detect and restart tasks
- **Future:** Upgrade to StatefulSet or CRIU checkpointing

#### No Visual Session Persistence

- Desktop environment resets on restart
- Open windows/applications lost
- **Impact:** Manual window arrangements not saved
- **Mitigation:** Agent can re-launch applications via MCP tools

#### Single MCP Transport

- Kagent only supports MCP protocol (no HTTP tools, no direct exec)
- **Impact:** All computer control must go through MCP server
- **Mitigation:** MCP is flexible, can wrap any capability

#### Resource Overhead

- Each agent = 3 pods (Agent + MCP + Desktop)
- Each agent = ~3.75 CPU cores + ~7.5Gi RAM (with desktop)
- **Impact:** Limited agents per cluster
- **Current capacity:** 5 agents max on current cluster
- **Mitigation:** CLI-only agents use ~1.75 CPU + 3.5Gi RAM

### Kagent Tool Limitations

**Only 2 Tool Types:**

1. **McpServer** - MCP protocol only
2. **Agent** - Other agents as tools

**No support for:**

- ❌ HTTP/REST tools
- ❌ Direct exec/subprocess
- ❌ Native Python functions
- ❌ OpenAPI integration (unless via MCP proxy)

**Workaround:** Create custom MCP servers to wrap any capability

## References

### Code Repositories

- **Kagent:** `/code/github.com/kagent-dev/kagent`
- **vnc-use:** `/code/github.com/mayflower/vnc-use`
- **computer-control-mcp:** `/code/github.com/AB498/computer-control-mcp` ✅
  - Verified working setup with nix-shell
  - See `shell.nix` and `RUN_ME.sh` for quick start
  - Test script: `simple_mcp_test.py`
- **Anthropic quickstarts:** (TODO: clone reference patterns)

### Documentation

- **Kagent Docs:** <https://kagent.dev/docs>
- **MCP Specification:** <https://modelcontextprotocol.io>
- **Agent Sandbox:** <https://github.com/kubernetes-sigs/agent-sandbox>
- **computer-control-mcp:** <https://github.com/AB498/computer-control-mcp>

### Related plan.md Sections

- Guacamole + Authentik RAC (browser-based desktop access)
- Agent Sandbox future enhancement
- Rook-Ceph for RWX storage (if multiple pods need shared filesystem)

## Success Criteria

**Phase 1 Success:**

- Agent can execute shell commands persistently
- Workspace survives agent pod restart
- Credentials properly injected and functional
- Agent can clone repos, run builds, commit results

**Phase 2 Success:**

- Agent can take screenshots and see desktop state
- Agent can click UI elements by coordinates
- Agent can type text and navigate applications
- Full computer control loop works (observe → decide → act)

**Phase 3 Success:**

- Multiple agents running concurrently
- Each agent isolated (network, credentials, workspace)
- Per-agent Helm chart deployment works
- Can deploy new agent in <5 minutes

**Production Ready:**

- Monitoring and alerting configured
- Backup/restore procedures documented
- Resource quotas prevent runaway agents
- Security policies enforced (network isolation, RBAC)
- Operational runbooks created
