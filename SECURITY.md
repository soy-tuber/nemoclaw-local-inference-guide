# Security Considerations

## What this repository exposes

This repository documents how to route inference requests from an
[NVIDIA NemoClaw](https://docs.nvidia.com/nemoclaw/) sandbox to a
**locally-hosted vLLM** instance over a Docker bridge network.

It contains:

- A TCP relay script (`relay.py`) that binds to a sandbox-internal veth
  interface and forwards traffic to the Docker bridge.
- Helper scripts (`ask`, `review`) that call the OpenAI-compatible API on the
  relay endpoint.
- A partial network policy (`policy-local.yaml`) that whitelists the local
  inference endpoints inside the sandbox.

## What this repository does NOT expose

- No credentials, API keys, or tokens. The local vLLM instance does not
  require authentication (`Bearer dummy` is a placeholder).
- No public-facing endpoints. All IP addresses in `env.example` are
  **RFC 1918 private addresses** (e.g. `172.18.x.x`, `10.200.x.x`) and are
  unreachable from the internet.
- No host-specific identifiers. Environment-specific values (bridge interface
  names, IPs, sandbox names) are parameterized via environment variables.

## Best practices

1. **Never commit `.env` files.** The `.gitignore` already excludes `.env`.
   Always use `env.example` as a template and keep your actual `.env` local.

2. **Review `policy-local.yaml` before applying.** The provided policy is a
   starting point. Remove any endpoints you do not need. In particular:
   - `172.17.0.1` (default Docker bridge) may not be required in your setup.
   - `host.docker.internal` is only relevant on Docker Desktop.
   - `integrate.api.nvidia.com` is for NVIDIA's cloud inference API; remove it
     if you only use local inference.

3. **Restrict `iptables` rules.** The guide uses broad ACCEPT rules on the
   Docker bridge for simplicity. In production, scope them to specific source
   IPs and ports.

4. **Keep the relay on the internal veth only.** `relay.py` should bind to the
   sandbox veth IP (default `10.200.0.1`), never to `0.0.0.0`.

5. **Sandbox isolation is your primary security boundary.** The network hacks
   in this guide intentionally punch holes in the sandbox's network isolation.
   Understand the trade-offs before applying them.
