# ai-node local Qwen Docker deployment

This fork pins Ollama, grants only the Ollama service NVIDIA GPU access, and
keeps Qwen data in the `ollama_data` volume. TradingAgents remains an
on-demand CLI container and publishes no host port.

```bash
cp .env.ollama.example .env
docker compose --profile ollama up -d ollama
docker compose exec ollama ollama pull qwen3:14b
docker compose --profile ollama run --rm tradingagents-ollama
```

Verify GPU-backed inference with:

```bash
docker compose exec ollama ollama ps
nvidia-smi
```
