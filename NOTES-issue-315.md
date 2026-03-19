# Notes on Issue #315 — NemoClaw Local vLLM Inference via WSL2

## Original Problem

Running NemoClaw sandbox with local vLLM inference on WSL2 + RTX 5090 required a
3-layer network hack: host iptables rules, a TCP relay in the pod's main namespace,
and iptables injection into the sandbox namespace via nsenter.

## Root Cause

NemoClaw's architecture already had the correct inference routing design:
- `inference.local` as a gateway-proxied endpoint accessible from inside the sandbox
- `host.openshell.internal` (OpenShell 0.0.10+) for gateway-to-host resolution

However, two issues prevented users from reaching this path:

1. **Onboard bug**: `nemoclaw onboard` did not correctly configure the sandbox for
   local vLLM. The model ID was hardcoded to `vllm-local` instead of auto-detecting
   from `/v1/models`, and the sandbox OpenClaw config was written with a direct vLLM
   URL instead of `https://inference.local/v1`. (Fixed in PR #412)

2. **Documentation gap**: The `inference.local` proxy mechanism and `host.openshell.internal`
   DNS entry were not documented. Users had no way to discover the intended routing
   path without reading the source code.

## Resolution

jieunl24 (NVIDIA) responded to Issue #315 with:
- A clear explanation of the intended architecture (gateway proxies inference, sandbox
  never needs direct vLLM access)
- PR #412: auto-detect vLLM model ID and route through `inference.local`
- PR #380: fix onboarding to not write direct URLs into sandbox config

## Assessment

This was a **design incompleteness**, not a design flaw. The routing architecture was
sound, but the onboard tooling — the primary entry point for users — failed to set it
up correctly. Combined with missing documentation, users were forced to reverse-engineer
workarounds (the 3-layer hack) for a problem that the platform had already solved internally.

A correct design that cannot be correctly deployed through the standard setup path is,
in effect, an incomplete design.

## References

- Issue #315: https://github.com/NVIDIA/NemoClaw/issues/315
- Issue #305: https://github.com/NVIDIA/NemoClaw/issues/305
- PR #412: https://github.com/NVIDIA/NemoClaw/pull/412
- PR #380: https://github.com/NVIDIA/NemoClaw/pull/380
- Blog post: https://media.patentllm.org/en/blog/gpu-inference/nemoclaw-local-vllm-sandbox
