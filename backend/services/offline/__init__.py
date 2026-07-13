"""
Offline mode — fully isolated local-model pipeline (Qwen2.5-Coder:7b via Ollama).

This package is deliberately self-contained: it does NOT import from or modify
backend/services/pipeline.py's Claude/OpenAI code paths. The only integration
point is a single dispatch check in backend/routers/chat.py that routes to
run_offline_stream() instead of the Claude/GPT pipeline when offline mode is
active. See OFFLINE_MODE.md for the evidence-based design constraints.
"""
