# NemoClaw Local Inference Guide

Route inference requests from an [NVIDIA NemoClaw](https://docs.nvidia.com/nemoclaw/)
(OpenShell) sandbox to a **locally-hosted vLLM** instance on an RTX GPU, bypassing
the sandbox's network isolation via a 3-layer networking hack.

**Model:** `nvidia/NVIDIA-Nemotron-Nano-9B-v2-Japanese` (Thinking model)
**API:** vLLM OpenAI-compatible (port 8000)

> Blog post (Japanese): *coming soon*

---

## Network Topology

```
┌─────────────────────────────────────────────────────────────────┐
│ WSL2 Host                                                       │
│   vLLM listening on 0.0.0.0:$VLLM_PORT                         │
│                                                                 │
│   Docker bridge: $BRIDGE_IFACE ($DOCKER_BRIDGE_IP)              │
│       │                                                         │
│       ▼                                                         │
│   ┌─────────────────────────────────────────────────────┐       │
│   │ openshell-cluster-$GATEWAY_NAME                     │       │
│   │   k3s cluster                                       │       │
│   │                                                     │       │
│   │   ┌───────────────────────────────────────────┐     │       │
│   │   │ Pod: $SANDBOX_NAME (10.42.0.x)            │     │       │
│   │   │   Main namespace (agent-sandbox-controller)│     │       │
│   │   │     TCP relay: $RELAY_BIND_IP:$VLLM_PORT  │     │       │
│   │   │       → $DOCKER_BRIDGE_IP:$VLLM_PORT      │     │       │
│   │   │                                           │     │       │
│   │   │   ┌─────────────────────────────────┐     │     │       │
│   │   │   │ Sandbox namespace (10.200.0.2)  │     │     │       │
│   │   │   │   ~/ask  → $RELAY_BIND_IP:$VLLM_PORT │     │       │
│   │   │   │   ~/review → same                │     │     │       │
│   │   │   └─────────────────────────────────┘     │     │       │
│   │   └───────────────────────────────────────────┘     │       │
│   └─────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────┘
```

Communication path:
```
sandbox (10.200.0.2) → relay ($RELAY_BIND_IP:$VLLM_PORT)
  → bridge ($DOCKER_BRIDGE_IP:$VLLM_PORT) → vLLM
```

---

## Prerequisites

- WSL2 host with an NVIDIA GPU and vLLM running
- Docker with the NemoClaw (OpenShell) cluster
- `openshell` CLI installed ([docs](https://docs.nvidia.com/nemoclaw/))

---

## Setup

### 0. Environment Variables

Copy the template and fill in your values:

```bash
cp config/env.example .env
# Edit .env — see comments in the file for how to find each value
source .env
```

### 1. Install openshell CLI

Follow the [official docs](https://docs.nvidia.com/nemoclaw/).

```bash
openshell --version    # e.g. openshell 0.0.7
```

### 2. Create Gateway

```bash
openshell gateway start --name "$GATEWAY_NAME" --port 9000
openshell gateway info -g "$GATEWAY_NAME"
```

### 3. Create Provider (local vLLM)

```bash
openshell provider create \
  --name local-vllm \
  --type openai \
  --config "OPENAI_BASE_URL=http://${DOCKER_BRIDGE_IP}:${VLLM_PORT}/v1" \
  -g "$GATEWAY_NAME"
```

The `OPENAI_BASE_URL` must be reachable from the openshell server pod (i.e.
the Docker bridge IP, not `localhost`). No API key is needed for local vLLM.

### 4. Create Sandbox

```bash
openshell sandbox create \
  --name "$SANDBOX_NAME" \
  --provider local-vllm \
  --policy policy.yaml \
  -g "$GATEWAY_NAME"

openshell sandbox list -g "$GATEWAY_NAME"    # Wait for Phase: Ready
```

### 5. Enter the Sandbox

```bash
openshell term -g "$GATEWAY_NAME"
# Select your sandbox → [s] for shell

# Or directly:
openshell sandbox connect "$SANDBOX_NAME" -g "$GATEWAY_NAME"
```

---

## Network Hacks

After the sandbox is running, apply these steps to allow traffic from the
sandbox to your host's vLLM.

### 5.1 Host iptables

Allow traffic from the Docker bridge to vLLM:

```bash
sudo iptables -I DOCKER-USER 1 \
  -i "$BRIDGE_IFACE" -p tcp --dport "$VLLM_PORT" -j ACCEPT

sudo iptables -I FORWARD 1 \
  -i "$BRIDGE_IFACE" -o eth0 -p tcp --dport "$VLLM_PORT" -j ACCEPT
```

Verify:
```bash
sudo iptables -L DOCKER-USER -n --line-numbers
sudo iptables -L FORWARD -n --line-numbers
```

### 5.2 Update Network Policy

Edit `config/policy-local.yaml` — replace the `${...}` placeholders with your
actual IPs from `.env`, then apply:

```bash
openshell policy set "$SANDBOX_NAME" -g "$GATEWAY_NAME" \
  --policy config/policy-local.yaml --wait --timeout 30
```

### 5.3 TCP Relay (Pod main namespace)

Deploy `relay.py` into the pod's main namespace. This bridges traffic from the
sandbox veth (`$RELAY_BIND_IP`) to the Docker bridge (`$DOCKER_BRIDGE_IP`).

```bash
docker exec "openshell-cluster-${GATEWAY_NAME}" kubectl exec "$SANDBOX_NAME" -n openshell -- \
  bash -c "cat > /tmp/relay.py << 'PYEOF'
$(cat scripts/relay.py)
PYEOF
DOCKER_BRIDGE_IP=${DOCKER_BRIDGE_IP} RELAY_BIND_IP=${RELAY_BIND_IP} VLLM_PORT=${VLLM_PORT} \
  nohup python3 /tmp/relay.py > /tmp/relay.log 2>&1 &"
```

### 5.4 Sandbox namespace iptables

The sandbox namespace has REJECT rules that block traffic to the relay.
Insert an ACCEPT rule:

```bash
# Get sandbox PID
SANDBOX_PID=$(docker exec "openshell-cluster-${GATEWAY_NAME}" \
  kubectl exec "$SANDBOX_NAME" -n openshell -- \
  cat /var/run/sandbox.pid)

# Inject iptables rule into sandbox namespace
docker exec "openshell-cluster-${GATEWAY_NAME}" \
  kubectl exec "$SANDBOX_NAME" -n openshell -- \
  nsenter -t "$SANDBOX_PID" -n iptables -I OUTPUT 1 \
  -d "$RELAY_BIND_IP" -p tcp --dport "$VLLM_PORT" -j ACCEPT
```

### 5.5 Deploy Tools into Sandbox

```bash
source .env
./scripts/setup-sandbox.sh
```

This uploads `ask` and `review` to `/sandbox/` inside the sandbox.

---

## Daily Startup

After WSL2/Docker restart, the volatile state (iptables, relay, sandbox
iptables) is lost. Re-apply:

1. **Verify vLLM** is running:
   ```bash
   curl -s http://localhost:${VLLM_PORT}/v1/models | python3 -c "import sys,json; print(json.load(sys.stdin))"
   ```

2. **Host iptables** (Section 5.1)

3. **Start NemoClaw terminal:**
   ```bash
   openshell term -g "$GATEWAY_NAME"
   ```

4. **TCP relay** — re-deploy if the container restarted (Section 5.3)

5. **Sandbox iptables** — re-inject if the sandbox restarted (Section 5.4)

6. **Re-deploy tools** if missing (Section 5.5)

7. **Test** (inside sandbox):
   ```bash
   no_proxy="*" curl -s http://${RELAY_BIND_IP}:${VLLM_PORT}/v1/models
   ~/ask "Hello, are you working?"
   ```

---

## Usage

### Ask a question
```bash
~/ask "Write a Python Fibonacci generator"
```

### Code review
```bash
~/review app.py
```

### opencode agent (inside sandbox)
```bash
export no_proxy="*" && opencode
# Then give natural language instructions — opencode will use tool calls
```

### Git clone (inside sandbox)
```bash
git -c http.sslVerify=false clone https://github.com/user/repo.git
```

### Port forwarding (for web apps)
```bash
# Inside sandbox:
python3 -m flask run --host=0.0.0.0 --port=5000 &

# On host:
openshell forward start 5000 "$SANDBOX_NAME" -g "$GATEWAY_NAME"
# Open http://localhost:5000 in browser
```

### File transfer
```bash
# Host → sandbox:
openshell sandbox upload "$SANDBOX_NAME" ./file.txt /sandbox/ -g "$GATEWAY_NAME"

# Sandbox → host:
openshell sandbox download "$SANDBOX_NAME" /sandbox/file.txt ./ -g "$GATEWAY_NAME"
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `curl` times out instantly / Connection refused | Sandbox iptables REJECT rule | Re-inject ACCEPT rule (Section 5.4) |
| `curl` times out after a few seconds | Relay stopped or host iptables missing | Check relay: `docker exec ... ps aux \| grep relay.py`; check iptables |
| `content` is `null` | Nemotron is a Thinking model; tokens consumed by `reasoning_content` | Set `max_tokens >= 4096`; check both `content` and `reasoning_content` |
| Requests go through proxy | Missing `no_proxy="*"` | Always set `no_proxy="*"` before `curl`/`python` |
| `git clone` SSL error | Sandbox proxy intercepts TLS | Use `git -c http.sslVerify=false clone` |
| Bridge IP changed after restart | WSL2 reassigned IPs | Run `ip addr show $BRIDGE_IFACE` and update `.env` |

---

## Files

| File | Description |
|------|-------------|
| `config/env.example` | Environment variable template |
| `config/policy-local.yaml` | Network policy (nvidia_inference section only) |
| `scripts/relay.py` | TCP relay for Pod main namespace |
| `scripts/ask` | One-liner LLM question tool (bash) |
| `scripts/review` | Code review tool (Python) |
| `scripts/setup-sandbox.sh` | Deploy ask/review into sandbox |
| `SECURITY.md` | Security considerations |

---

## License

The network policy file (`policy-local.yaml`) is derived from NVIDIA's default
sandbox policy and is licensed under Apache-2.0. All other files in this
repository are provided as-is for educational purposes.
