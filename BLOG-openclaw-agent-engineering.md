# Building a Claude Code-Class Agent Inside a Sandbox: Lessons from NemoClaw + OpenClaw + Local vLLM

*March 20, 2026*

## Introduction

NVIDIA's [NemoClaw](https://github.com/NVIDIA/NemoClaw) runs [OpenClaw](https://openclaw.ai/) — a secure AI agent platform — inside [OpenShell](https://openshell.dev/) sandboxes, with configurable inference backends (NIM, vLLM, Ollama). All inference is routed through a gateway proxy so the sandboxed agent never needs direct network access to the host.

This post documents what we learned running NemoClaw on WSL2 with an RTX 5090, using Nemotron 9B v2 via vLLM. The focus is not on model quality, but on the **engineering required to build a capable agent** — routing, tool call parsing, system prompts, context injection, and the gap between "model can generate text" and "agent can do useful work."

The core thesis: **most of what makes Claude Code effective is not the model — it's the scaffolding.** And that scaffolding is largely missing from OpenClaw's default configuration.

Our research is based on [jieunl24's fork](https://github.com/jieunl24/NemoClaw), which contains bug fixes for local vLLM inference (PR #412, PR #380).

---

## 1. Architecture: How NemoClaw Routes Inference

NemoClaw's inference routing is clean and well-designed:

```
OpenClaw (sandbox)
  → https://inference.local/v1        (gateway-proxied endpoint)
    → OpenShell gateway
      → http://host.openshell.internal:8000/v1   (Docker host DNS)
        → vLLM Gateway (port 8000)
          → vLLM (port 8100, actual model)
```

The sandbox never contacts the host directly. The gateway resolves `host.openshell.internal` (OpenShell 0.0.10+) to the Docker host IP. No iptables, no TCP relays, no namespace injection needed.

Key routing code from the codebase:

```javascript
// bin/lib/inference-config.js
const INFERENCE_ROUTE_URL = "https://inference.local/v1"

// bin/lib/local-inference.js
function getLocalProviderBaseUrl(provider) {
  switch (provider) {
    case "vllm-local":  return `${HOST_GATEWAY_URL}:8000/v1`;
    case "ollama-local": return `${HOST_GATEWAY_URL}:11434/v1`;
  }
}
```

### Supported Inference Providers

| Provider | Type | Endpoint (from gateway) |
|----------|------|------------------------|
| **NVIDIA NIM** | Cloud API | `https://integrate.api.nvidia.com/v1` |
| **vLLM** | Local | `http://host.openshell.internal:8000/v1` |
| **Ollama** | Local | `http://host.openshell.internal:11434/v1` |

TensorRT-LLM, llama.cpp, and other inference servers are not directly supported. Indirect use through vLLM (as a backend engine) is possible.

Ollama's integration is notably more mature than vLLM's — it includes model auto-detection via `ollama list`, warmup commands with `keep_alive`, and health probes that validate model-level responses. vLLM integration only checks whether `/v1/models` responds. This likely reflects Ollama's broader adoption as a consumer-facing tool; vLLM users are expected to manage their own model lifecycle.

---

## 2. The Onboarding Bug: How We Got Here

When we first tried to run NemoClaw with local vLLM on WSL2 + RTX 5090, the sandbox couldn't reach vLLM. We built a 3-layer network hack — host iptables rules, a TCP relay in the pod's main namespace, and iptables injection into the sandbox via nsenter — just to get inference through. This "V1" worked, but was fragile and entirely volatile, requiring full reconfiguration on every WSL2/Docker restart.

We filed [Issue #315](https://github.com/NVIDIA/NemoClaw/issues/315). NVIDIA maintainer jieunl24 responded: **the routing architecture we had hacked around already existed.** Two bugs in `nemoclaw onboard` prevented users from reaching the intended path:

1. **Hardcoded model ID**: The onboard wizard wrote `vllm-local` as the model name instead of auto-detecting from `/v1/models`. OpenClaw couldn't match the model to a provider.

2. **Direct URL in sandbox config**: The onboard wizard wrote the raw vLLM URL into `openclaw.json`, bypassing `inference.local`. The sandbox couldn't resolve `host.openshell.internal` — only the gateway can.

Both were fixed in [PR #412](https://github.com/NVIDIA/NemoClaw/pull/412) and [PR #380](https://github.com/NVIDIA/NemoClaw/pull/380). With the fix, the 3-layer network hack and the simpler agent (opencode) we'd been using were no longer necessary. Our assessment: a **design incompleteness**, not a design flaw. The routing architecture was sound, but the onboarding tooling failed to set it up correctly. Combined with zero documentation about `inference.local`, users had no way to discover the intended path without reading the source code. (Full analysis in [NOTES-issue-315.md](./NOTES-issue-315.md).)

---

## 3. Tool Call and Reasoning Parsers

With routing solved, the next challenge is format translation. Nemotron 9B v2 does not output OpenAI-format tool calls or clean content/reasoning separation. Two vLLM parser plugins bridge this gap.

### 3.1 The Problem: `<TOOLCALL>` Format

Nemotron v2 emits tool calls as XML-wrapped JSON in plain text:

```
<TOOLCALL>[{"name": "get_weather", "arguments": {"city": "Tokyo"}}]</TOOLCALL>
```

OpenClaw expects OpenAI's structured format:

```json
{
  "tool_calls": [{
    "type": "function",
    "function": {
      "name": "get_weather",
      "arguments": "{\"city\": \"Tokyo\"}"
    }
  }],
  "finish_reason": "tool_calls"
}
```

A vLLM parser plugin converts between these at the server level. Similarly, the model wraps its reasoning in `<think>` tags, which need to be extracted into a separate `reasoning_content` field.

### 3.2 Where to Find the Parsers

NVIDIA provides official parser plugins for Nemotron v2. The authoritative source is the model's [vLLM Cookbook](https://github.com/NVIDIA-NeMo/Nemotron/blob/main/usage-cookbook/Nemotron-Nano-9B-v2/vllm_cookbook.ipynb) in the Nemotron repository, which documents the correct vLLM flags and parser configuration. The tool call parser implementation itself (`nemotron_toolcall_parser_streaming.py`, 637 lines, registered as `nemotron_json`) is hosted in the [NeMo repository](https://github.com/NVIDIA-NeMo/NeMo/blob/main/examples/voice_agent/server/parsers/nemotron_toolcall_parser_streaming.py).

The official parser provides:

- **Partial JSON parsing** via `partial_json_parser` — reconstructs valid objects from incomplete streaming fragments
- **Tag buffering** — suppresses ambiguous partial sequences (e.g., `<TOO`) to prevent leaking control tokens
- **Streaming delta computation** — computes precise diffs between current and previous JSON states
- **Multi-tool call support** with proper index tracking

We initially wrote a minimal 154-line regex-based parser before discovering the official one. It worked for basic cases, but lacked streaming robustness. After switching to NVIDIA's parser, tool calls work correctly through our vLLM gateway.

### 3.3 Why Not Built-In vLLM Parsers?

We tested whether vLLM's built-in parsers could replace the plugin approach. They cannot:

- **`qwen3_coder`** requires `<tool_call>` / `</tool_call>` as dedicated tokens in the tokenizer. Nemotron v2 doesn't have these — it outputs `<TOOLCALL>` as regular text tokens. The parser fails at startup: *"Qwen3 XML Tool parser could not locate tool call start/end tokens in the tokenizer!"*
- **`nemotron_v3`** is for the Nemotron 3 family (e.g., Nemotron-3-Nano-4B-FP8), a different model generation with different tokenizer and output formats.

The model's tool calling training data was generated using Qwen3 models, but the output format is `<TOOLCALL>` — a JSON-in-XML format specific to Nemotron v2, not `<tool_call>`. **Plugin-based parsers are the correct and official approach.** Always check the model's HuggingFace card — parser requirements vary per model family.

### 3.4 vLLM Startup Configuration

Putting it together, the vLLM serve command with both parsers:

```bash
vllm serve nvidia/NVIDIA-Nemotron-Nano-9B-v2-Japanese \
  --trust-remote-code \
  --port 8100 \
  --enable-auto-tool-choice \
  --tool-call-parser nemotron_json \
  --tool-parser-plugin nemotron_toolcall_parser_streaming.py \
  --reasoning-parser nemotron_nano_v2 \
  --reasoning-parser-plugin nemotron_nano_v2_reasoning_parser.py
```

The reasoning parser separates `<think>` content into a `reasoning_content` field so clients receive clean output. This is infrastructure Claude Code doesn't need — Anthropic's API handles it natively — but for local models, this parser chain is what makes the output consumable by agent frameworks.

---

## 4. On-Demand vLLM Gateway

Between the clients and vLLM, we run a lightweight gateway (`vllm_gateway.py`) on port 8000 that proxies requests to vLLM on port 8100. Its primary purpose is **VRAM management**: on a single RTX 5090 running multiple services, keeping the model loaded 24/7 is wasteful.

The gateway:
- **Starts vLLM on first request** and stops it after 10 minutes of idle time
- **Proxies all requests** transparently, including streaming SSE
- **Rewrites `<TOOLCALL>` tags** in non-streaming responses as a fallback (the vLLM parser handles streaming; the gateway catches anything that slips through)

This is independent of the parser plugins — it's a process lifecycle manager with a proxy, not a replacement for vLLM-level parsing.

---

## 5. The Agent Gap: What OpenClaw Ships vs. What Claude Code Does

With routing and parsing working, the agent runs. But the gap between OpenClaw's default behavior and Claude Code's effectiveness is vast.

OpenClaw provides:
- A TUI/web interface
- File system tools (read, write, edit, search)
- Shell command execution
- Gateway for inference routing
- System prompt files: `SOUL.md`, `USER.md`, `AGENTS.md`, `TOOLS.md`

**What it does NOT provide out of the box:**

1. **Structured system prompts** — The default `SOUL.md` is a generic personality template. No tool use patterns, no output formatting rules, no reasoning strategies.

2. **Context injection at request time** — Claude Code wraps every user message with project context (CLAUDE.md, git state, recent errors). OpenClaw reads its `.md` files at startup but doesn't enforce their content every turn.

3. **Programmatic prompt enforcement** — Writing instructions in markdown and hoping the model reads them is fragile. What works is code-level enforcement: injecting context into the message payload, not the system prompt file.

4. **Generation parameter tuning** — The default ships with `reasoning: false` and `maxTokens: 4096`. For a Thinking model, this is crippling. We had to set `reasoning: true` and `maxTokens: 65536` manually.

5. **Model-specific tool call guidance** — Claude Code's system prompt extensively documents each tool with examples. OpenClaw's `TOOLS.md` is an empty template.

### Evidence: The Morning Briefing Pattern

The same Nemotron 9B model, given proper prompting, produces excellent output. A daily briefing system uses the same model with temperature 0.4, structured prompts with explicit sections and formatting rules, a start tag that forces immediate generation, and programmatic context injection (7 days of Claude Code and Gemini CLI history, deduplicated and trimmed).

The result is a polished HTML briefing with accurate summaries and zero hallucination. The same model, given an open-ended instruction through OpenClaw's default prompt, hallucinated file contents and fabricated commands.

**The difference is not the model. It's the prompt engineering.**

---

## 6. Roadmap: Closing the Gap

Everything needed to bring OpenClaw closer to Claude Code already exists within NemoClaw's architecture. What's missing is the *content* — the behavioral instructions, context injection logic, and output validation that turns a model into an agent.

### 6.1 Structured System Prompts

Replace generic templates with model-specific, task-specific instructions:

```markdown
# Tool Usage Rules
When you need to read a file, ALWAYS use the read_file tool. NEVER guess file contents.
When editing files, read the file first. Never edit a file you haven't read.

# Output Format
- Lead with the action, not the reasoning.
- If a tool call fails, diagnose the error. Do not retry the same call.
```

This mirrors Claude Code's approach: hundreds of lines of specific behavioral rules, not a personality description.

### 6.2 Context Injection

Programmatically prepend project context to every user message:
```
[Working directory: /sandbox/project]
[Recent files: main.py (modified 2m ago)]
[Git status: 2 modified, 1 untracked]
[Active errors: TypeError in main.py:42]
```

Alternatively, register a `get_context` tool the agent calls per turn, or a hybrid of both.

### 6.3 Plugin Architecture

NemoClaw already supports OpenClaw plugins via `openclaw.plugin.json` manifests. This is the correct extension point for custom tools, context providers, output validators, and persistent memory.

### 6.4 Generation Parameters

Different tasks need different parameters:

| Task | Temperature | Max Tokens |
|------|-------------|------------|
| Code generation | 0.3-0.4 | 8192+ |
| Summarization | 0.4 | 6000 |
| Classification | 0.1 | 512-1024 |
| Tool-heavy workflows | 0.3 | 16384 |

### 6.5 Sandbox Resilience

The sandbox is fragile. Piping commands via `openshell sandbox connect` killed the running OpenClaw gateway. Config injection via pipe corrupted `openclaw.json`. Production agents need config snapshots, gateway auto-recovery, and documented workspace isolation.

---

## 7. Practical Takeaways

1. **`NEMOCLAW_EXPERIMENTAL=1`** is required for `nemoclaw onboard` to offer the vLLM provider option.

2. **inference.local routing works.** The 3-layer network hack from V1 is unnecessary. PR #412 fixed the onboarding bug.

3. **Use NVIDIA's official parser plugins** for Nemotron v2 tool calls and reasoning. Find them in the [Nemotron vLLM Cookbook](https://github.com/NVIDIA-NeMo/Nemotron/blob/main/usage-cookbook/Nemotron-Nano-9B-v2/vllm_cookbook.ipynb) and [NeMo repository](https://github.com/NVIDIA-NeMo/NeMo/blob/main/examples/voice_agent/server/parsers/nemotron_toolcall_parser_streaming.py). Built-in vLLM parsers (`qwen3_coder`, `nemotron_v3`) are incompatible with v2.

4. **Set `reasoning: true` and `maxTokens: 65536`** in `openclaw.json`. The defaults are wrong for Thinking models.

5. **Invest in system prompts.** The default `SOUL.md` is a starting point, not a solution. Add tool use rules, output format constraints, and behavioral instructions.

6. **Inject context programmatically.** Markdown files are read inconsistently. Wrap user messages with structured context at the code level.

7. **Temperature 0.4 with structured prompts beats temperature 0.7 with generic prompts** on the same model.

8. **Never pipe commands into `openshell sandbox connect`.** Use `openshell sandbox upload/download`. Pipe sessions kill running processes.

---

## References

- [NemoClaw Repository](https://github.com/NVIDIA/NemoClaw)
- [jieunl24's Fork](https://github.com/jieunl24/NemoClaw) (PR #412 source, used for this research)
- [Issue #315: WSL2 + RTX 5090 Local Inference](https://github.com/NVIDIA/NemoClaw/issues/315)
- [PR #412: Auto-detect vLLM model ID, route through inference.local](https://github.com/NVIDIA/NemoClaw/pull/412)
- [PR #380: Fix onboarding direct URL bug](https://github.com/NVIDIA/NemoClaw/pull/380)
- [Nemotron 9B v2 vLLM Cookbook](https://github.com/NVIDIA-NeMo/Nemotron/blob/main/usage-cookbook/Nemotron-Nano-9B-v2/vllm_cookbook.ipynb)
- [NVIDIA Official Tool Call Parser (NeMo)](https://github.com/NVIDIA-NeMo/NeMo/blob/main/examples/voice_agent/server/parsers/nemotron_toolcall_parser_streaming.py)
- [OpenShell Documentation](https://openshell.dev/)
- [OpenClaw Documentation](https://docs.openclaw.ai/)

---

*This post is based on hands-on research running NemoClaw on WSL2 (Ubuntu 24.04) with an NVIDIA GeForce RTX 5090, using nvidia/NVIDIA-Nemotron-Nano-9B-v2-Japanese served via vLLM with official NVIDIA parser plugins.*

*Incidentally, a look at the [NemoClaw commit history](https://github.com/jieunl24/NemoClaw/commits) reveals that NVIDIA's own maintainers are using Claude Code to contribute to the project. The tool that inspired this analysis is, it turns out, already part of the workflow on the other side.*
