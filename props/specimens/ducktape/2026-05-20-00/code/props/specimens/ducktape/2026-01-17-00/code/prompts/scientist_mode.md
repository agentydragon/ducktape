# Scientist Mode: Systematic Issue Resolution Framework

## Fuck This, We're Going Full Scientist Mode

When you've been doing individual checks one by one for hours and getting nowhere, it's time to stop the trial-and-error bullshit and go full systematic scientist mode.

## The Problem

Individual checks one by one are inefficient, error-prone, and lead to missing critical information. Time to get systematic.

## The Solution: Exhaustive Data Collection → Analysis → Fix Loop

### Phase 1: Comprehensive Information Gathering

Put together a very exhaustive list of **all checks and information gathering** you could possibly want for information about this issue. Write a parallelized Python script with timeouts around all operations that collects under a common directory with timestamped subdirs full **unredacted, unfiltered outputs** of all diagnostic commands.

### Phase 2: Scientific Method Loop - NO MANUAL CHECKS

Operate in the **"collect → analyze → update script → repeat until fixed"** loop:

1. **Run collection script** → gather timestamped data like a scientist in a detailed lab notebook
2. **Read info from new information** → what did we learn?
3. **CRITICAL: Update script** to add any new checks you discover, then run script again
4. **NEVER do manual checks** - always add them to the script first, then run
5. **If you found the problem, try to fix it**
6. **Repeat until fixed**

**🚫 DO NOT DO MANUAL CHECKS ONCE**
**✅ TEACH THE SCRIPT to do it automatically and run it forever into the future automatically all in ONE turnkey command**

### Phase 3: Lab Notebook Documentation

Keep a **timestamped append-at-bottom markdown file** with:

- Steps you're trying
- High-level things you've learned
- Always reason about at each step:
  - What are high value information sources you are not yet collecting or checking?
  - What information could you collect that you aren't yet collecting?
  - Have you read the source code of components?
  - Do you understand how everything works?
  - What observability knobs do you have you haven't yet turned on?

## Implementation Guidelines

### Investigation Folder Structure

Use permanent storage structure (NOT temporary folders):

```
investigations/
  <issue_name>/
    LAB_NOTEBOOK.md
    <collection_script>.py
    observations/
      YYYY-MM-DD-HHMMSS/
        <all collected data>
```

### Data Collection Script Requirements

- **Maximum parallelism** - use asyncio to run all operations concurrently with individual timeouts
- **DRY and modular design** - create reusable building blocks like:
  - `run_command_with_timeout(cmd, timeout)` - execute shell commands with capture
  - `dump_k8s_job_status(job_name, namespace)` - comprehensive Kubernetes job diagnostics
  - `dump_api_status(url, description)` - API endpoint testing with validation
  - `collect_logs(service, container, lines)` - standardized log collection
- **Timestamped subdirectories** for each run
- **Unredacted, unfiltered outputs** - capture everything (no privacy/redaction by default)
- **Graceful degradation** in absence of elevated privileges
- **Use elevated privileges when available** to gather more comprehensive data
- **Easily extensible** - add new checks as you discover more areas to investigate
- **Progressive enhancement** - as you zoom in on ideas of what could be wrong, progressively update collection script to include automated checks for such conditions
- **Future-oriented maintenance** - treat as a longer-lived diagnostic tool that will evolve over time

### Source Code Investigation

Source code management:

- **Clone only once** - source code dumps should not be re-done on every collection run
- **Discover location first**: Before cloning, manually identify the idiomatic local location for source code
- **Get user confirmation**: In the confirmation/plan stage, confirm with user where source code should be placed before starting clones
- Clone only one copy, preferring conventional system-wide mirror location
- Try in order: 1. `/mnt/tankshare/code`, 2. `~/code` (whichever exists first)
- Follow local conventions
- Make sure you are looking at the right pinned versions (check deployed version vs source)
- **Reuse existing clones** - check if repositories already exist before cloning

**Avoid speculating/trial-and-error** - if you're working with open source, **just clone the damn code**. Go after the ground truth:

- GitHub MCP server if enabled
- GitHub API
- Clone repositories
- Search online documentation
- Read the fucking source code

### Scientific Rigor

- **No guessing** - gather data first
- **Document everything** - timestamp all actions and observations
- **Understand root causes** - don't just fix symptoms
- **Systematic progression** - each iteration should build on previous knowledge
- **Question assumptions** - what do you think you know that might be wrong?

## Example Exhaustive Check Categories

### System Level

- Process lists and resource usage
- Network connections and routing
- File system permissions and disk usage
- System logs and kernel messages
- Environment variables and configuration files

### Application Level

- Service logs (all components, all verbosity levels)
- Configuration dumps (live config vs static config)
- Database queries and schema inspection
- API endpoint testing (all endpoints, all methods)
- Cache states and memory dumps

### Infrastructure Level

- Kubernetes resources and events
- Container states and resource limits
- Network policies and ingress rules
- Secret and ConfigMap contents (redacted appropriately)
- Helm release states and values

### Authentication/Authorization

- User/role/permission mappings
- Token validity and scopes
- Certificate chains and validity
- OIDC flows and redirect chains
- Session states and cookies

### Source Code Analysis

- Clone all relevant repositories
- Understand authentication flows
- Identify configuration precedence
- Find undocumented configuration options
- Understand error handling and logging

### Observability: TURN ON ALL THE KNOBS

**GATHER ALL THE LOGS, TURN ON ALL THE KNOBS**

**IMPORTANT**: Turning on debug logging and observability knobs is a **system mutation** that should be done **outside** the data collection script. Create a separate script for putting the system into high-observability mode, but document/automate this process.

**High-Observability Setup** (separate script/process):

- Enable debug/trace logging on ALL components
- Increase log verbosity to maximum levels
- Enable audit logging where available
- Turn on performance monitoring and tracing
- Enable all diagnostic endpoints
- Enable SQL query logging and slow query logs
- Turn on garbage collection logging
- Enable memory profiling and heap dumps
- Enable metrics collection and export
- Turn on health check endpoints with detailed responses

**Data Collection** (read-only collection script):

- Capture all available logs at current verbosity levels
- Collect existing diagnostic outputs and metrics
- Gather network traffic and packet dumps
- Capture existing thread dumps and stack traces
- Document current observability settings

### Data Hoarding Mode

Collect **EVERYTHING**:

- All log files (current + rotated)
- All configuration files (static + dynamic)
- All environment variables and runtime parameters
- All database dumps and schema definitions
- All container images and their manifests
- All network configurations and routing tables
- All certificate chains and cryptographic materials
- All performance metrics and resource utilization data
- All error messages and stack traces
- All API responses and request/response pairs

## The Goal

Stop fucking around with random individual checks. Become a data-driven scientist who:

1. **Collects comprehensive evidence** before forming hypotheses
2. **Documents every observation** with timestamps
3. **Reads the source code** to understand ground truth
4. **Tests hypotheses systematically** with reproducible experiments
5. **Fixes root causes** not just symptoms
6. **GATHERS ALL THE LOGS AND TURNS ON ALL THE KNOBS**

This is how you solve complex distributed systems problems efficiently.
