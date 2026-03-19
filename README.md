# NemoClaw Local Inference Guide (V2)

Route inference requests from an [NVIDIA NemoClaw](https://github.com/NVIDIA/NemoClaw)
(OpenShell) sandbox to a **locally-hosted vLLM** instance on an NVIDIA GPU,
using the built-in `inference.local` gateway proxy.

**Model:** `nvidia/NVIDIA-Nemotron-Nano-9B-v2-Japanese` (Thinking model)
**API:** vLLM OpenAI-compatible (port 8000 = Gateway, port 8100 = vLLM internal)
**Agent:** [OpenClaw](https://openclaw.ai/) — secure AI agent platform running inside the sandbox

> **Blog post:** [Building a Claude Code-Class Agent Inside a Sandbox](./BLOG-openclaw-agent-engineering.md)
> **Issue #315 analysis:** [NOTES-issue-315.md](./NOTES-issue-315.md)
> **V1 (legacy 3-layer hack):** [old/README-v1.md](./old/README-v1.md)

---

## V1 → V2 Changes

**V1 (3-layer network hack):**
- Host iptables rules to allow Docker bridge → vLLM traffic
- TCP relay (`relay.py`) in the pod's main namespace
- `nsenter` to inject iptables ACCEPT rules into the sandbox namespace
- Communication: sandbox → 10.200.0.1:8000 → relay → 172.18.0.1:8000 → Gateway → vLLM
- Used `opencode` (simple coding TUI) + custom `~/ask`, `~/review` tools
- Entirely volatile — required full reconfiguration on every WSL2/Docker restart

**V2 (inference.local routing):**
- Uses OpenShell 0.0.10+ `host.openshell.internal` DNS
- Gateway proxies inference: sandbox → `inference.local` → vLLM
- No iptables, no relay, no nsenter
- OpenClaw (full AI agent) runs inside the sandbox
- `nemoclaw onboard` handles setup automatically (with [PR #412](https://github.com/NVIDIA/NemoClaw/pull/412) patch)

---

## Inference Routing

```
┌─────────────────────────────────────────────────────────────────┐
│ WSL2 Host                                                       │
│   vLLM Gateway (0.0.0.0:8000) → vLLM (localhost:8100)          │
│                                                                 │
│   OpenShell Gateway (port 9000)                                 │
│       │                                                         │
│       │  host.openshell.internal:8000                           │
│       │  (= Docker host IP, gateway routes to vLLM)             │
│       │                                                         │
│   ┌───┴─────────────────────────────────────────────────┐       │
│   │ openshell-cluster (k3s)                             │       │
│   │                                                     │       │
│   │   ┌───────────────────────────────────────────┐     │       │
│   │   │ Sandbox                                   │     │       │
│   │   │                                           │     │       │
│   │   │   OpenClaw TUI                            │     │       │
│   │   │     → https://inference.local/v1          │     │       │
│   │   │       → (gateway proxy)                   │     │       │
│   │   │         → host.openshell.internal:8000    │     │       │
│   │   │           → vLLM Gateway → vLLM           │     │       │
│   │   │                                           │     │       │
│   │   └───────────────────────────────────────────┘     │       │
│   └─────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────┘
```

Communication path:
```
sandbox (OpenClaw) → https://inference.local/v1
  → OpenShell gateway proxy
    → http://host.openshell.internal:8000/v1
      → vLLM Gateway (0.0.0.0:8000)
        → vLLM (localhost:8100)
```

No iptables, TCP relay, or nsenter required.

---

## Prerequisites

- WSL2 + Ubuntu (tested on 24.04)
- NVIDIA GPU with sufficient VRAM for the model
- Docker running
- vLLM serving on port 8000 (or via `vllm_gateway.py`)
- Node.js >= 20, npm >= 10

---

## Installation

### 1. Install NemoClaw

```bash
cd ~/Projects/NemoClaw
npm install && npm link
nemoclaw --version
```

### 2. Apply PR #412 Patch

Copy the fixed files from [jieunl24's fork](https://github.com/jieunl24/NemoClaw):

```bash
git clone https://github.com/jieunl24/NemoClaw.git ~/Projects/NemoClaw-jieunl24
cd ~/Projects/NemoClaw-jieunl24
git checkout fix/vllm-onboard-model-detection

cp ~/Projects/NemoClaw-jieunl24/bin/lib/onboard.js ~/Projects/NemoClaw/bin/lib/
cp ~/Projects/NemoClaw-jieunl24/bin/lib/local-inference.js ~/Projects/NemoClaw/bin/lib/
cp ~/Projects/NemoClaw-jieunl24/bin/lib/inference-config.js ~/Projects/NemoClaw/bin/lib/
```

### 3. Fix Port Conflicts (if needed)

If the default gateway port 8080 conflicts with other services, edit `onboard.js`:

```javascript
// Change 1: preflight check port
{ port: 9000, label: "OpenShell gateway" },

// Change 2: add --port to gateway start command
run(`openshell gateway start --port 9000 ${gwArgs.join(" ")}`, ...);
```

### 4. Update OpenShell

```bash
curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
openshell --version    # Must be 0.0.10+ for host.openshell.internal
```

### 5. Run Onboard

Ensure vLLM is running on port 8000, then:

```bash
curl -s http://localhost:8000/v1/models    # Verify vLLM responds

NEMOCLAW_EXPERIMENTAL=1 nemoclaw onboard
```

- `NEMOCLAW_EXPERIMENTAL=1` is required to enable the vLLM provider option
- Select "Local vLLM" when prompted
- The model is auto-detected from vLLM's `/v1/models` endpoint

### 6. Manual Setup (if onboard fails partway)

If the gateway and sandbox were created but provider/inference configuration is incomplete:

```bash
# Create vLLM provider (run on host)
openshell provider create --name vllm-local --type openai \
  --credential "OPENAI_API_KEY=dummy" \
  --config "OPENAI_BASE_URL=http://host.openshell.internal:8000/v1" \
  -g <gateway-name>

# Set inference route (run on host)
openshell inference set --no-verify --provider vllm-local \
  --model "nvidia/NVIDIA-Nemotron-Nano-9B-v2-Japanese" \
  -g <gateway-name>
```

Do **not** manually write config files inside the sandbox. The sandbox config is auto-generated during onboard Step 3 (sandbox creation). See Troubleshooting section 9A.

---

## Daily Startup

V2 eliminates all volatile hacks. If the gateway and sandbox exist, only vLLM needs to be running.

**Step 1:** Verify vLLM is running
```bash
curl -s http://localhost:8000/v1/models
curl -s http://localhost:8000/gateway/status    # If using vLLM Gateway
```

**Step 2:** Check gateway
```bash
openshell gateway info -g <gateway-name>
# If stopped:
openshell gateway start --port 9000 --name <gateway-name>
```

**Step 3:** Connect to sandbox and start OpenClaw
```bash
openshell sandbox connect <sandbox-name>
openclaw tui
```

That's it. No iptables, no relay.

### Recovery: "Missing gateway auth token"

If `openclaw tui` fails with this error, the sandbox's OpenClaw gateway process has died. Inside the sandbox:

```bash
nohup openclaw gateway run > /tmp/gateway.log 2>&1 &
sleep 2
cat /tmp/gateway.log    # Check for errors
openclaw tui
```

If `gateway.mode=local (current: unset)` error appears, the `openclaw.json` gateway section was corrupted. Fix it by uploading a repair script from the host:

```bash
# On host — create fix script
cat > /tmp/fix_gateway.py << 'EOF'
import json
p = '/sandbox/.openclaw/openclaw.json'
c = json.load(open(p))
c['gateway'] = {
    'mode': 'local',
    'controlUi': {
        'allowInsecureAuth': True,
        'dangerouslyDisableDeviceAuth': True,
        'allowedOrigins': ['http://127.0.0.1:18789']
    },
    'trustedProxies': ['127.0.0.1', '::1']
}
json.dump(c, open(p, 'w'), indent=2)
print('done')
EOF

openshell sandbox upload <sandbox-name> /tmp/fix_gateway.py /sandbox/ -g <gateway-name>

# In sandbox:
python3 /sandbox/fix_gateway.py
nohup openclaw gateway run > /tmp/gateway.log 2>&1 &
openclaw tui
```

---

## Usage

### OpenClaw TUI

```bash
# Inside sandbox:
openclaw tui

# Natural language instructions:
#   "Write hello to test.txt"
#   "Read the contents of test.txt"
#   "List files in current directory"
#   "Create a Python script that..."
```

OpenClaw operates only within the sandbox filesystem (security isolation).

### File Transfer

```bash
# Host → sandbox:
openshell sandbox upload <sandbox-name> ./file.txt /sandbox/ -g <gateway-name>

# Sandbox → host:
openshell sandbox download <sandbox-name> /sandbox/file.txt ./ -g <gateway-name>
```

### Port Forwarding

```bash
# Inside sandbox:
python3 -m http.server 5000

# On host:
openshell forward start 5000 <sandbox-name> -g <gateway-name>
# Open http://localhost:5000 in browser
```

### Git Clone (inside sandbox)

```bash
git -c http.sslVerify=false clone https://github.com/user/repo.git
```

---

## Verification

```bash
# Test inference.local from inside sandbox
printf 'curl -s https://inference.local/v1/models\n' | openshell sandbox connect <sandbox-name>

# Test chat completions from inside sandbox
printf 'curl -s https://inference.local/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '"'"'{"model":"nvidia/NVIDIA-Nemotron-Nano-9B-v2-Japanese","messages":[{"role":"user","content":"hello"}],"max_tokens":100}'"'"'\n' | openshell sandbox connect <sandbox-name>

# Check OpenClaw version
printf 'openclaw --version\n' | openshell sandbox connect <sandbox-name>

# Gateway info
openshell gateway info -g <gateway-name>

# Provider list
openshell provider list -g <gateway-name>
```

---

## Troubleshooting

### Things You Must NOT Do

1. **Do not manually write config files inside the sandbox.** `openclaw.json` is auto-generated by onboard. Manual edits can remove the `gateway` section and break `openclaw gateway`. If a fix is needed, create a script on the host, upload it with `openshell sandbox upload`, and run it inside the sandbox.

2. **Do not pipe commands via `openshell sandbox connect` while `openclaw tui` is running.** This kills the running OpenClaw gateway process. Use `openshell sandbox upload/download` for file transfer.

3. **Do not run `openshell gateway destroy` casually.** This deletes all sandbox data with no recovery. To restart the gateway: `openshell gateway stop` → `openshell gateway start`. If only the in-sandbox OpenClaw gateway died, restart it inside the sandbox with `nohup openclaw gateway run`.

### FAQ

| Problem | Cause | Solution |
|---------|-------|----------|
| Cannot reach inference.local | Gateway stopped or provider not configured | `openshell gateway info`, `openshell provider list`; see Section 6 (Manual Setup) |
| `nemoclaw onboard` says "Port 8080 is not available" | Port conflict with another service | Change port in onboard.js (see Section 3) |
| vLLM option not shown during onboard | `NEMOCLAW_EXPERIMENTAL=1` not set, or vLLM not running | Set the env var; verify `curl http://localhost:8000/v1/models` |
| `content` is `null` in response | Nemotron is a Thinking model; reasoning consumes tokens | Set `max_tokens >= 4096` |
| OpenClaw doesn't execute tool calls | Config baseUrl is not `inference.local` | Verify sandbox config points to `https://inference.local/v1` |
| `git clone` SSL error | Sandbox proxy intercepts TLS | `git -c http.sslVerify=false clone <url>` |
| `nemoclaw onboard` module not found | PR #412 files not copied | Copy all 3 files (onboard.js, local-inference.js, inference-config.js) |
| "Missing gateway auth token" | Sandbox OpenClaw gateway process died | See Recovery section above |
| "gateway.mode=local (current: unset)" | `openclaw.json` gateway section corrupted | See Recovery section above |
| OpenClaw hallucinates commands/paths | Model accuracy drops in long outputs | Keep instructions short; set `reasoning: true`, `maxTokens: 65536`; add explicit constraints to SOUL.md |
| OpenClaw can't access files outside workspace | Tools are restricted to `~/.openclaw/workspace/` | Copy files into the workspace directory |

---

## NemoClaw Directory Structure

```
~/Projects/NemoClaw/
├── nemoclaw                    ← CLI entry point
├── bin/
│   ├── nemoclaw.js             ← CLI main script
│   └── lib/
│       ├── onboard.js          ← nemoclaw onboard (setup wizard)
│       ├── inference-config.js ← inference.local routing definition (PR #412)
│       ├── local-inference.js  ← host.openshell.internal URL definition (PR #412)
│       ├── runner.js           ← Sandbox launch/management
│       ├── preflight.js        ← Prerequisite checks
│       ├── policies.js         ← Policy management
│       ├── credentials.js      ← Credential management
│       ├── nim.js              ← NIM provider
│       ├── platform.js         ← Platform detection
│       ├── registry.js         ← Registry operations
│       └── resolve-openshell.js ← OpenShell path resolution
├── scripts/
│   ├── nemoclaw-start.sh       ← Sandbox entry point (openclaw config + startup)
│   ├── setup.sh                ← Initial setup
│   ├── install.sh              ← Installer
│   ├── start-services.sh       ← Service startup
│   ├── test-inference-local.sh ← Local inference test
│   ├── test-inference.sh       ← Inference test
│   └── write-auth-profile.py   ← Auth profile writer
├── docs/                       ← Official documentation
├── test/                       ← Tests
├── Dockerfile                  ← Sandbox container definition
├── package.json                ← Node.js dependencies
└── pyproject.toml              ← Python dependencies
```

---

## vLLM Gateway and Parser Setup

The vLLM gateway (`vllm_gateway.py`) sits between clients and the vLLM server, providing:

- **On-demand model loading** — starts vLLM on first request, stops after 10 minutes idle (VRAM auto-release)
- **`<TOOLCALL>` rewriting** — converts Nemotron's XML-format tool calls to OpenAI-compatible `tool_calls`
- **Streaming SSE buffering** — buffers streaming responses to detect and rewrite tool calls

### vLLM Startup Command

```bash
vllm serve nvidia/NVIDIA-Nemotron-Nano-9B-v2-Japanese \
  --trust-remote-code \
  --mamba_ssm_cache_dtype float32 \
  --max-num-seqs 64 \
  --port 8100 \
  --reasoning-parser-plugin nemotron_nano_v2_reasoning_parser.py \
  --reasoning-parser nemotron_nano_v2 \
  --enable-auto-tool-choice \
  --tool-call-parser nemotron_json \
  --tool-parser-plugin nemotron_toolcall_parser_streaming.py \
  --host 0.0.0.0
```

### Parser Plugins

| Parser | File | Purpose |
|--------|------|---------|
| `nemotron_json` | `nemotron_toolcall_parser_streaming.py` | Converts `<TOOLCALL>` XML to structured `tool_calls`. [NVIDIA official parser from NeMo repo](https://github.com/NVIDIA-NeMo/NeMo/blob/main/examples/voice_agent/server/parsers/nemotron_toolcall_parser_streaming.py). |
| `nemotron_nano_v2` | `nemotron_nano_v2_reasoning_parser.py` | Extracts `<think>` reasoning tags into a separate `reasoning_content` field. |

**Why not built-in vLLM parsers?** Nemotron v2 uses `<TOOLCALL>` as regular text tokens. The `qwen3_coder` built-in parser requires `<tool_call>`/`</tool_call>` as dedicated tokenizer tokens, which Nemotron v2 lacks. The `nemotron_v3` parser is for the Nemotron 3 model family (different generation). Plugin-based parsers are the correct and official approach for Nemotron v2. See the [blog post](./BLOG-openclaw-agent-engineering.md#33-why-not-built-in-vllm-parsers) for details.

---

## References

- [NemoClaw Repository](https://github.com/NVIDIA/NemoClaw)
- [jieunl24's Fork](https://github.com/jieunl24/NemoClaw) (PR #412 source)
- [Issue #315: WSL2 + RTX 5090 Local Inference](https://github.com/NVIDIA/NemoClaw/issues/315)
- [PR #412: Auto-detect vLLM model ID, route through inference.local](https://github.com/NVIDIA/NemoClaw/pull/412)
- [PR #380: Fix onboarding direct URL bug](https://github.com/NVIDIA/NemoClaw/pull/380)
- [NVIDIA Official Tool Call Parser (NeMo)](https://github.com/NVIDIA-NeMo/NeMo/blob/main/examples/voice_agent/server/parsers/nemotron_toolcall_parser_streaming.py)
- [Nemotron 9B v2 vLLM Cookbook](https://github.com/NVIDIA-NeMo/Nemotron/blob/main/usage-cookbook/Nemotron-Nano-9B-v2/vllm_cookbook.ipynb)
- [OpenShell Documentation](https://openshell.dev/)
- [OpenClaw Documentation](https://docs.openclaw.ai/)

---

## License

The V1 network policy file (`old/config/policy-local.yaml`) is derived from NVIDIA's default
sandbox policy and is licensed under Apache-2.0. All other files in this repository are
provided as-is for educational purposes.
