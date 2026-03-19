# Building a Claude Code-Class Agent Inside a Sandbox: Lessons from NemoClaw + OpenClaw + Local vLLM

*March 20, 2026*

## Introduction

NVIDIA's [NemoClaw](https://github.com/NVIDIA/NemoClaw) is an orchestration layer that runs [OpenClaw](https://openclaw.ai/) — a secure AI agent platform — inside [OpenShell](https://openshell.dev/) sandboxes, with configurable inference backends. It supports NVIDIA NIM (cloud), vLLM (local), and Ollama (local), routing all inference through a gateway proxy so the sandboxed agent never needs direct network access to the host. Our research is based on [jieunl24's fork](https://github.com/jieunl24/NemoClaw), which contains PR #412 and other fixes for local vLLM inference.

This post documents what we learned running NemoClaw on WSL2 with an RTX 5090, using a local Nemotron 9B model via vLLM. The focus is not on model quality per se, but on the **engineering required to build a capable agent** — system prompts, context injection, tool call infrastructure, and the architectural gap between "model can generate text" and "agent can do useful work."

The core thesis: **most of what makes Claude Code effective is not the model — it's the scaffolding.** And that scaffolding is largely missing from OpenClaw's default configuration.

---

## 1. Architecture: How NemoClaw Routes Inference

NemoClaw's inference routing is clean and well-designed (after PR #412 fixed the onboarding bug):

```
OpenClaw (sandbox)
  → https://inference.local/v1        (gateway-proxied endpoint)
    → OpenShell gateway
      → http://host.openshell.internal:8000/v1   (Docker host DNS)
        → vLLM Gateway (port 8000)
          → vLLM (port 8100, actual model)
```

Key components from the codebase:

**`bin/lib/inference-config.js`** defines the routing constant:
```javascript
const INFERENCE_ROUTE_URL = "https://inference.local/v1"
```

**`bin/lib/local-inference.js`** maps providers to host URLs:
```javascript
function getLocalProviderBaseUrl(provider) {
  switch (provider) {
    case "vllm-local":  return `${HOST_GATEWAY_URL}:8000/v1`;
    case "ollama-local": return `${HOST_GATEWAY_URL}:11434/v1`;
  }
}
```

**`scripts/nemoclaw-start.sh`** configures the sandbox on boot:
```bash
fix_openclaw_config()
  # Sets gateway.mode = 'local'
  # Sets allowInsecureAuth, dangerouslyDisableDeviceAuth
  # Sets allowedOrigins, trustedProxies
```

The sandbox never contacts the host directly. The gateway handles DNS resolution via `host.openshell.internal` (available in OpenShell 0.0.10+), which resolves to the Docker host IP from within the gateway container. This eliminates the need for iptables rules, TCP relays, or namespace injection — all of which were required in our V1 setup.

### Supported Inference Providers

NemoClaw supports exactly three inference backends, defined in `local-inference.js` and `inference-config.js`:

| Provider | Type | Endpoint (from gateway) |
|----------|------|------------------------|
| **NVIDIA NIM** | Cloud API | `https://integrate.api.nvidia.com/v1` |
| **vLLM** | Local | `http://host.openshell.internal:8000/v1` |
| **Ollama** | Local | `http://host.openshell.internal:11434/v1` |

**TensorRT-LLM, llama.cpp, and other inference servers are not directly supported.** However, since vLLM can use TensorRT-LLM as a backend engine, indirect use through vLLM is possible.

Ollama's integration is notably thorough compared to vLLM. The codebase includes:

- **Model auto-detection** via `ollama list`, parsed into selectable options during onboard
- **Warmup commands** that pre-load the model with a `keep_alive` parameter (default 15 minutes)
- **Health probes** that send a test prompt and validate the JSON response, catching not just connectivity failures but model-level errors
- **Default model fallback** to `nemotron-3-nano:30b` if no models are detected

```javascript
// Ollama warmup: keeps model loaded in memory for 15 minutes
getOllamaWarmupCommand(model, keepAlive = "15m")
  → curl -s http://localhost:11434/api/generate
      -d '{"model": model, "prompt": "hello", "stream": false, "keep_alive": "15m"}'

// Ollama probe: validates model actually responds correctly
validateOllamaModel(model, runCapture)
  → Sends test prompt, parses JSON, checks for error field
  → Returns { ok: false, message: "model did not answer in time" } on failure
```

vLLM integration, by contrast, only checks whether `/v1/models` responds — no warmup, no probe, no model selection UI. This likely reflects Ollama's broader adoption as a consumer-facing tool; vLLM users are expected to manage their own model lifecycle.

**Lesson**: A well-designed routing architecture is invisible when it works. But that's only half the story — we had to find the bugs that prevented it from working in the first place.

---

## 2. The Onboarding Bug: How We Got Here

When we first attempted to run NemoClaw with local vLLM on WSL2 + RTX 5090, nothing worked. The sandbox couldn't reach vLLM. We built a 3-layer network hack (host iptables rules, a TCP relay in the pod's main namespace, and iptables injection into the sandbox via nsenter) just to get inference requests through. This was our V1 — functional, but fragile and entirely volatile, requiring full reconfiguration on every WSL2 or Docker restart.

We filed [Issue #315](https://github.com/NVIDIA/NemoClaw/issues/315) documenting the problem and our workaround. NVIDIA maintainer jieunl24 responded with a surprising answer: **the routing architecture we had hacked around already existed.** The `inference.local` gateway proxy and `host.openshell.internal` DNS were designed for exactly this use case. Two bugs in `nemoclaw onboard` prevented users from reaching this path:

1. **Hardcoded model ID**: The onboard wizard wrote `vllm-local` as the model identifier instead of auto-detecting the actual model name from vLLM's `/v1/models` endpoint. OpenClaw couldn't match the model to a provider.

2. **Direct URL in sandbox config**: The onboard wizard wrote the raw vLLM URL (e.g., `http://host.openshell.internal:8000/v1`) directly into the sandbox's `openclaw.json`, bypassing the `inference.local` proxy entirely. The sandbox couldn't resolve this URL because it has no direct access to `host.openshell.internal` — only the gateway does.

Both bugs were fixed in [PR #412](https://github.com/NVIDIA/NemoClaw/pull/412) and [PR #380](https://github.com/NVIDIA/NemoClaw/pull/380). The fix was straightforward: auto-detect the model ID from `/v1/models`, and always route through `https://inference.local/v1` instead of writing direct URLs.

The onboarding bug had a cascading effect on our approach. Because the intended path was broken, we fell into a V1 workflow that required the 3-layer network hack and a simpler agent (opencode instead of OpenClaw). The opencode detour was entirely a consequence of the bug. The custom vLLM parsers, however, turned out to be **necessary regardless** — we verified that vLLM's built-in parsers (`qwen3_coder`, `nemotron_v3`) are incompatible with Nemotron v2's tokenizer and output format (see Section 3.4). NVIDIA's own deployment guide for this model also uses custom plugin parsers. So: the network hack and opencode were unnecessary detours caused by the bug, but the parser work was essential and would have been needed on the correct path too.

Our assessment: this was a **design incompleteness**, not a design flaw. The routing architecture was sound, but the onboarding tooling — the primary entry point for users — failed to set it up correctly. Combined with zero documentation about `inference.local` or `host.openshell.internal`, users had no way to discover the intended path without reading the source code. A correct design that cannot be correctly deployed through the standard setup path is, in effect, an incomplete design. (Full analysis in [NOTES-issue-315.md](./NOTES-issue-315.md).)

---

## 3. The vLLM Gateway: More Than a Proxy

Our vLLM setup uses a custom gateway (`vllm_gateway.py`) that sits between clients and the vLLM server. It does three critical things:

### 2.1 On-Demand Model Loading

The gateway starts vLLM only when the first request arrives and stops it after 10 minutes of idle time. On a single RTX 5090 running multiple services, VRAM is a shared resource — keeping the model loaded 24/7 is wasteful.

```python
VLLM_CMD = [
    ".venv/bin/vllm", "serve",
    "nvidia/NVIDIA-Nemotron-Nano-9B-v2-Japanese",
    "--trust-remote-code",
    "--port", "8100",
    "--reasoning-parser-plugin", "nemotron_nano_v2_reasoning_parser.py",
    "--reasoning-parser", "nemotron_nano_v2",
    "--enable-auto-tool-choice",
    "--tool-call-parser", "nemotron_json",
    "--tool-parser-plugin", "nemotron_toolcall_parser_streaming.py",
]
```

### 3.2 NVIDIA's Official Tool Call Parser

Nemotron 9B v2 emits tool calls in a `<TOOLCALL>` XML format rather than OpenAI's native function calling format. A parser plugin converts these at the vLLM server level:

**Model output:**
```
<TOOLCALL>[{"name": "get_weather", "arguments": {"city": "Tokyo"}}]</TOOLCALL>
```

**Parser output (OpenAI format):**
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

We initially wrote a minimal custom parser (154 lines) that handled this with regex. It worked, but lacked robustness for streaming and edge cases. We later found NVIDIA's official parser in the [NeMo repository](https://github.com/NVIDIA-NeMo/NeMo/blob/main/examples/voice_agent/server/parsers/nemotron_toolcall_parser_streaming.py) (637 lines, registered as `nemotron_json`), which provides:

- **Partial JSON parsing** via `partial_json_parser` — reconstructs valid objects from incomplete JSON fragments during streaming
- **Tag buffering** — suppresses ambiguous partial tag sequences (e.g., `<TOO`) to prevent leaking control tokens to the client
- **Streaming delta computation** — computes the precise difference between current and previous JSON states for monotonic streaming
- **Multi-tool call support** — manages an array of tool calls with proper index tracking
- **Auto-closer stripping** — removes premature `}` and `]` characters that `partial_json_parser` adds to incomplete JSON

We switched to NVIDIA's official parser and verified it works correctly with the 9B v2 Japanese model through our vLLM gateway. The model's tool calling training data was generated using Qwen3 models, but the output format is `<TOOLCALL>` — a JSON-in-XML format specific to Nemotron v2.

### 3.3 Why Not Built-In vLLM Parsers?

We tested whether vLLM's built-in parsers could replace the plugin approach. They cannot:

- **`qwen3_coder`** requires `<tool_call>` / `</tool_call>` as dedicated tokens in the model's tokenizer. Nemotron v2 does not have these — it outputs `<TOOLCALL>` as regular text tokens. The parser fails at startup: *"Qwen3 XML Tool parser could not locate tool call start/end tokens in the tokenizer!"*
- **`nemotron_v3`** is for the Nemotron 3 model family (e.g., Nemotron-3-Nano-4B-FP8), which is a different generation with different tokenizer and output formats. It does not exist in vLLM 0.15.1 and is not compatible with v2.

**Plugin-based parsers are the correct and official approach for Nemotron v2.** Always check the model's HuggingFace card for the correct parser — it varies per model family.

### 3.4 Reasoning Tag Extraction

The reasoning parser (`nemotron_nano_v2_reasoning_parser.py`) separates `<think>` content from the model's response at the server level. This means:

- All clients receive clean content without thinking tags
- Reasoning is available in a separate `reasoning_content` field
- Streaming chunks carry both fields independently

**This is infrastructure that Claude Code doesn't need** — Anthropic's API handles it natively. But for local models, this parser chain is what makes the output consumable by downstream agent frameworks.

---

## 4. The Agent Gap: What OpenClaw Ships vs. What Claude Code Does

Here's where it gets interesting. OpenClaw, as deployed by NemoClaw, provides:

- A TUI/web interface for interacting with the agent
- File system tools (read, write, edit, search)
- Shell command execution
- A gateway for device pairing and inference routing
- System prompt files: `SOUL.md`, `USER.md`, `AGENTS.md`, `TOOLS.md`

**What it does NOT provide out of the box:**

1. **Structured system prompts optimized for the actual model** — The default `SOUL.md` is a generic personality template ("Be genuinely helpful, not performatively helpful"). It contains no instructions about tool use patterns, output formatting, or reasoning strategies.

2. **Context injection at request time** — Claude Code wraps every user message with project context (CLAUDE.md, recent file contents, git state). OpenClaw reads its `.md` files but doesn't enforce their content in every turn.

3. **Programmatic prompt enforcement** — Writing instructions in a markdown file and hoping the model reads them is fragile. Even Claude Opus doesn't reliably follow long markdown instructions. What works is code-level enforcement: injecting context into the message payload, not the system prompt file.

4. **Temperature and generation parameter tuning** — The default `openclaw.json` ships with `reasoning: false` and `maxTokens: 4096`. For a Thinking model, this is crippling. We had to manually set `reasoning: true` and `maxTokens: 65536`.

5. **Model-specific tool call guidance** — Claude Code's system prompt extensively documents how to use each tool, with examples. OpenClaw's `TOOLS.md` is an empty template.

### Evidence: The Morning Briefing Pattern

We have direct evidence that the same Nemotron 9B model, given proper prompting, produces excellent output. A daily morning briefing system (`morning_briefing.py`) uses the same model with:

- **Temperature 0.4** (not 0.7)
- **max_tokens 6000** (not 4096)
- **Structured prompt with 6 explicit sections** and formatting rules
- **A start tag** (`BEGIN_HTML_BRIEFING`) that forces the model to begin generating immediately
- **Post-processing** that strips any leaked `<think>` tags
- **Programmatic context injection**: 7 days of Claude Code and Gemini CLI history, deduplicated and trimmed

The result is a polished, well-organized HTML briefing with accurate project summaries, actionable suggestions, and zero hallucination about recent work. The same model, given an open-ended "scan this project" instruction through OpenClaw's default prompt, hallucinated file contents and fabricated commands.

**The difference is not the model. It's the prompt engineering.**

---

## 5. Bridging the Gap: A Roadmap for Claude Code-Class OpenClaw Agents

Based on our research, here's what would be needed to bring OpenClaw agent quality closer to Claude Code:

### 4.1 Structured System Prompts (SOUL.md / AGENTS.md)

Replace the generic templates with model-specific, task-specific instructions:

```markdown
# Tool Usage Rules

When you need to read a file, ALWAYS use the read_file tool. NEVER guess file contents.
When you need to search, use grep_search first. If results are insufficient, use glob_search.
When editing files, read the file first. Never edit a file you haven't read.

# Output Format

- Lead with the action, not the reasoning.
- When showing code changes, show the specific edit, not the entire file.
- If a tool call fails, diagnose the error. Do not retry the same call.

# Reasoning

You are a Thinking model. Use <think> tags to reason through complex problems.
Always think before making tool calls with side effects.
```

This mirrors how Claude Code's system prompt works — it doesn't just say "be helpful," it provides hundreds of lines of specific behavioral instructions for tool use, output formatting, and error handling.

### 4.2 Context Injection Layer

Claude Code injects project context into every request: `CLAUDE.md` contents, git status, recent errors, file contents from recent edits. OpenClaw needs an equivalent mechanism:

**Option A: Pre-prompt wrapper** — Before every user message, programmatically prepend context:
```
[Current directory: /sandbox/project]
[Recent files: main.py (modified 2m ago), utils.py (read 5m ago)]
[Git status: 2 files modified, 1 untracked]
[Active errors: TypeError in main.py:42]

User's actual message here.
```

**Option B: Tool-driven context** — Register a `get_context` tool that the agent calls at the start of each turn. This is more flexible but requires the model to learn to call it reliably.

**Option C: Hybrid** — Inject minimal context (working directory, last error) in every turn, and provide a `deep_context` tool for when the agent needs more.

### 4.3 Plugin Architecture

NemoClaw already supports OpenClaw plugins (`openclaw plugins install /opt/nemoclaw`). The plugin is a Node.js package with an `openclaw.plugin.json` manifest. This is the correct extension point for:

- **Custom tools**: Project-specific commands (deploy, test, lint)
- **Context providers**: Hooks that inject context before each turn
- **Output validators**: Post-processing that catches hallucinated paths or commands
- **Memory management**: Persistent context across sessions (like Claude Code's `CLAUDE.md` updates)

The Dockerfile pre-installs the NemoClaw plugin:
```dockerfile
COPY nemoclaw/dist/ /opt/nemoclaw/dist/
COPY nemoclaw/openclaw.plugin.json /opt/nemoclaw/
RUN openclaw plugins install /opt/nemoclaw > /dev/null 2>&1 || true
```

Custom plugins could follow the same pattern — build, copy into the image, and register during sandbox creation.

### 4.4 Generation Parameters as First-Class Config

The `openclaw.json` model configuration needs to be treated as a tuning surface, not a default:

```json
{
  "models": {
    "providers": {
      "inference": {
        "models": [{
          "id": "nvidia/NVIDIA-Nemotron-Nano-9B-v2-Japanese",
          "reasoning": true,
          "maxTokens": 65536
        }]
      }
    }
  }
}
```

Different tasks need different parameters. Our findings across multiple use cases:

| Task | Temperature | Max Tokens | Quality Impact |
|------|-------------|------------|----------------|
| Code generation / editing | 0.3-0.4 | 8192+ | High precision needed |
| Project scanning / summarization | 0.4 | 6000 | Structured output |
| Classification / routing | 0.1 | 512-1024 | Deterministic |
| Creative / open-ended | 0.7 | 4096 | Default is fine |
| Tool-heavy workflows | 0.3 | 16384 | Low temp prevents tool hallucination |

OpenClaw should expose per-task temperature profiles, or at minimum, allow the system prompt to influence generation parameters.

### 4.5 Sandbox Resilience

One finding from our session: **the sandbox is fragile.** Sending a file via `openshell sandbox connect` (pipe mode) killed the running OpenClaw gateway process. Injecting config via a piped command corrupted `openclaw.json` by removing the `gateway` section. Recovery required writing a Python fix script, uploading it via `openshell sandbox upload`, and executing it from within the sandbox.

For a production agent:

- **Config snapshots**: Automatic backup of `openclaw.json` before any modification
- **Gateway auto-recovery**: If the gateway process dies, systemd-style restart
- **Workspace isolation**: OpenClaw tools should only access `~/.openclaw/workspace/Projects/`, which is already the case — but this needs to be documented and enforced for file uploads too

---

## 6. The Philosophical Point

The difference between "a model that can generate text" and "an agent that can do useful work" is enormous. It's the difference between having an engine and having a car.

Claude Code is a car. It has:
- A chassis (system prompt with hundreds of behavioral rules)
- A transmission (context injection that feeds the model what it needs, every turn)
- Steering (tool definitions with examples and constraints)
- Brakes (output validation, safety checks, confirmation prompts)
- A dashboard (status updates, error reporting)

OpenClaw, as currently configured by NemoClaw, is closer to an engine on a test stand. The engine works — our Nemotron 9B produces excellent output when properly prompted (the morning briefing proves this). But the surrounding infrastructure assumes the model will figure things out from a personality description in `SOUL.md`.

This is not a criticism of OpenClaw's design. It's an observation about what's needed to close the gap. Claude Code has years of prompt engineering baked into its system prompt. OpenClaw provides the *platform* — the sandbox, the tool infrastructure, the gateway routing — but leaves the *prompt engineering* to the user.

The good news: everything needed to close this gap exists within NemoClaw's architecture. The plugin system, the config injection in `nemoclaw-start.sh`, the model provider configuration — these are all the right extension points. What's missing is the *content*: the hundreds of lines of behavioral instructions, the context injection logic, the parameter tuning, and the output validation that turns a model into an agent.

---

## 7. Practical Takeaways

1. **`nemoclaw onboard` with `NEMOCLAW_EXPERIMENTAL=1`** is required for vLLM provider selection. Without it, only NIM (cloud) is offered.

2. **inference.local routing works.** The 3-layer iptables/relay/nsenter hack from V1 is completely unnecessary. PR #412 fixed the onboarding bug that prevented this from being set up automatically.

3. **Custom vLLM parsers are essential** for local models. The `<TOOLCALL>` and `<think>` tag parsers bridge the gap between Nemotron's native output format and OpenAI's API format that OpenClaw expects.

4. **Default generation parameters are wrong for Thinking models.** Set `reasoning: true` and `maxTokens: 65536` (or higher) in `openclaw.json`.

5. **System prompt files need real content.** The default `SOUL.md` template is a starting point, not a solution. Invest in model-specific tool use instructions, output formatting rules, and explicit behavioral constraints.

6. **Context injection must be programmatic.** Markdown files are read inconsistently. Wrap user messages with structured context at the code level.

7. **Temperature matters more than model size.** The same 9B model at temperature 0.4 with structured prompts outperforms itself at temperature 0.7 with generic prompts.

8. **Never pipe commands into `openshell sandbox connect`.** Use `openshell sandbox upload` / `download` for file transfer. Pipe sessions can kill running processes inside the sandbox.

---

## References

- [NemoClaw Repository](https://github.com/NVIDIA/NemoClaw)
- [jieunl24's Fork](https://github.com/jieunl24/NemoClaw) (PR #412 source, used for this research)
- [Issue #315: WSL2 + RTX 5090 Local Inference](https://github.com/NVIDIA/NemoClaw/issues/315)
- [PR #412: Auto-detect vLLM model ID, route through inference.local](https://github.com/NVIDIA/NemoClaw/pull/412)
- [PR #380: Fix onboarding direct URL bug](https://github.com/NVIDIA/NemoClaw/pull/380)
- [NVIDIA Official Tool Call Parser (NeMo)](https://github.com/NVIDIA-NeMo/NeMo/blob/main/examples/voice_agent/server/parsers/nemotron_toolcall_parser_streaming.py)
- [Nemotron 9B v2 vLLM Cookbook](https://github.com/NVIDIA-NeMo/Nemotron/blob/main/usage-cookbook/Nemotron-Nano-9B-v2/vllm_cookbook.ipynb)
- [OpenShell Documentation](https://openshell.dev/)
- [OpenClaw Documentation](https://docs.openclaw.ai/)

---

*This post is based on hands-on research running NemoClaw on WSL2 (Ubuntu 24.04) with an NVIDIA GeForce RTX 5090, using nvidia/NVIDIA-Nemotron-Nano-9B-v2-Japanese served via vLLM with custom tool call and reasoning parsers.*

*Incidentally, a look at the [NemoClaw commit history](https://github.com/jieunl24/NemoClaw/commits) reveals that NVIDIA's own maintainers are using Claude Code to contribute to the project. The tool that inspired this analysis is, it turns out, already part of the workflow on the other side.*
