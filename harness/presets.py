"""Ready-made connections for Settings > Connections > Add. Each is a template: pick one, paste a key if it needs one,
adjust the model name. Any OpenAI-compatible server works with "Custom" too.

Fields: kind (see config.py), base (OpenAI-style /v1 root), model (a sensible default; the user can change it),
key_help (where to get a key), llama (llama.cpp extras: prompt cache, Qwen-style thinking switch), efforts (Fast/Think/
Deep -> reasoning_effort), level_models (Fast/Think/Deep -> a different model), sampling (sent with every request)."""

import os

PRESETS = [
    # API keys (plug in your own key and go)
    {"preset": "anthropic", "group": "API key", "name": "Anthropic API (Claude)", "kind": "anthropic",
     "base": "https://api.anthropic.com/v1", "model": "claude-sonnet-5-5", "context": 200_000, "max_output": 16_384,
     "vision": True, "key_env": "ANTHROPIC_API_KEY",
     "help": "Key from console.anthropic.com. Uses the native Anthropic Messages API with prompt caching and extended thinking."},
    {"preset": "openai", "group": "API key", "name": "OpenAI API", "kind": "openai", "base": "https://api.openai.com/v1",
     "model": "gpt-6.2", "context": 400_000, "vision": True, "efforts": {"fast": "low", "think": "medium", "deep": "high"},
     "key_env": "OPENAI_API_KEY", "help": "Key from platform.openai.com (billed per token, separate from a ChatGPT plan)."},
    {"preset": "gemini", "group": "API key", "name": "Gemini", "kind": "openai",
     "base": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-flash-latest", "context": 250_000,
     "vision": True, "efforts": {"fast": "low", "think": "medium", "deep": "high"}, "key_env": "GEMINI_API_KEY",
     "help": "Key from aistudio.google.com (Google AI Studio)."},
    {"preset": "deepseek", "group": "API key", "name": "DeepSeek", "kind": "openai", "base": "https://api.deepseek.com/v1",
     "model": "deepseek-chat", "context": 128_000, "level_models": {"think": "deepseek-reasoner", "deep": "deepseek-reasoner"},
     "key_env": "DEEPSEEK_API_KEY", "help": "Key from platform.deepseek.com. Fast uses deepseek-chat, Think and Deep deepseek-reasoner."},
    {"preset": "openrouter", "group": "API key", "name": "OpenRouter", "kind": "openai", "base": "https://openrouter.ai/api/v1",
     "model": "deepseek/deepseek-chat", "context": 128_000, "key_env": "OPENROUTER_API_KEY",
     "help": "One key for hundreds of models (openrouter.ai/keys). Model names look like vendor/model."},
    {"preset": "groq", "group": "API key", "name": "Groq", "kind": "openai", "base": "https://api.groq.com/openai/v1",
     "model": "openai/gpt-oss-120b", "context": 128_000, "key_env": "GROQ_API_KEY", "help": "Key from console.groq.com."},
    {"preset": "mistral", "group": "API key", "name": "Mistral", "kind": "openai", "base": "https://api.mistral.ai/v1",
     "model": "mistral-medium-latest", "context": 128_000, "key_env": "MISTRAL_API_KEY", "help": "Key from console.mistral.ai."},
    {"preset": "together", "group": "API key", "name": "Together AI", "kind": "openai", "base": "https://api.together.xyz/v1",
     "model": "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8", "context": 128_000, "key_env": "TOGETHER_API_KEY",
     "help": "Key from api.together.ai."},
    {"preset": "orcarouter", "group": "API key", "name": "OrcaRouter", "kind": "openai", "base": "https://api.orcarouter.ai/v1",
     "model": "", "context": 128_000, "key_env": "ORCAROUTER_API_KEY",
     "help": "An OrcaRouter key (sk-orca-...). Name the model, e.g. orca/orcacyber-flash-1.0. Some of its models are not listed "
             "in /v1/models but still answer."},
    # plans you already pay for, through their own command-line tools (no API key)
    {"preset": "claude-plan", "group": "Your subscription", "name": "Claude (your plan, via Claude Code)", "kind": "claude-code",
     "model": "", "context": 200_000,
     "help": "Uses the Claude Code CLI and the Claude plan you are signed in with. Install it (npm i -g @anthropic-ai/claude-code), "
             "run `claude` once to sign in. Leave the model empty for Claude Code's default, or name one (e.g. opus, sonnet). "
             "The top bar then offers Claude Code's effort levels (Low to Max)."},
    {"preset": "chatgpt-plan", "group": "Your subscription", "name": "ChatGPT (your plan, via Codex)", "kind": "codex",
     "model": "", "context": 272_000,
     "help": "Uses the Codex CLI and the ChatGPT plan you are signed in with. Install it (npm i -g @openai/codex), run "
             "`codex login` once. Leave the model empty for Codex's default. The top bar then offers Codex's reasoning "
             "levels (Low to Max)."},
    {"preset": "agy-plan", "group": "Your subscription", "name": "Gemini (via Antigravity CLI)", "kind": "agy",
     "model": "gemini-4-neon", "context": 1_000_000, "effort": "high",
     "help": "Uses Google's Antigravity CLI (`agy`) on your signed-in plan. Run `agy models` to see available models."},
    {"preset": "opencode-go", "group": "Your subscription", "name": "OpenCode Go", "kind": "opencode-go",
     "base": "https://opencode.ai/zen/go/v1", "model": "deepseek-v4.1-flash", "context": 250_000, "vision": True,
     "help": "An OpenCode Go key (opencode.ai). Several keys, one per line, fail over when one runs out."},
    # models on your own machine or server
    {"preset": "ollama", "group": "Local model", "name": "Ollama", "kind": "openai", "base": "http://127.0.0.1:11434/v1",
     "model": "qwen3:8b", "context": 32_768, "help": "ollama.com. Pull a model first (ollama pull qwen3:8b); set its context "
             "in Ollama too (OLLAMA_CONTEXT_LENGTH), the default is small."},
    {"preset": "lmstudio", "group": "Local model", "name": "LM Studio", "kind": "openai", "base": "http://127.0.0.1:1234/v1",
     "model": "", "context": 32_768, "help": "Start the server in LM Studio (Developer tab); the model name is shown there."},
    {"preset": "llamacpp", "group": "Local model", "name": "llama.cpp server", "kind": "openai", "base": "http://127.0.0.1:8080/v1",
     "model": "local", "context": 131_072, "llama": True,
     "help": "llama-server -m model.gguf --jinja -c 131072 --port 8080. --jinja is needed for tool calls."},
    {"preset": "vllm", "group": "Local model", "name": "vLLM", "kind": "openai", "base": "http://127.0.0.1:8000/v1",
     "model": "", "context": 131_072, "help": "vllm serve <model> --enable-auto-tool-choice --tool-call-parser <parser>."},
    {"preset": "custom", "group": "Other", "name": "Custom OpenAI-compatible", "kind": "openai", "base": "http://127.0.0.1:8080/v1",
     "model": "", "context": 128_000, "help": "Anything that speaks the OpenAI chat completions API with tool calls."},
]


def public():
    out = []
    for p in PRESETS:
        item = dict(p)
        env_name = item.get("key_env")
        item["env_key_present"] = bool(env_name and os.environ.get(env_name))
        out.append(item)
    return out
