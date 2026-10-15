# ai-node local Qwen Docker deployment

This fork pins Ollama, grants only the Ollama service NVIDIA GPU access, and
keeps Qwen data in the `ollama_data` volume. TradingAgents remains an
on-demand CLI container and publishes no host port.

Create the local environment file and add a dedicated Alpaca paper-account key.
Never commit this file or paste the key into logs:

```bash
cp .env.ollama.example .env
chmod 600 .env
nano .env
```

Set these two values in `.env`:

```dotenv
APCA_API_KEY_ID=your-paper-key-id
APCA_API_SECRET_KEY=your-paper-secret
```

Ticker news combines Alpaca and Yahoo, removes duplicate URLs/headlines, and
includes a bounded plain-text Alpaca article-body excerpt when Alpaca supplies
one. Global news and insider transactions remain on Yahoo. When Alpaca
credentials are absent or Alpaca is unavailable, ticker news continues with
Yahoo.

Build and run the updated containers:

```bash
docker compose build tradingagents tradingagents-ollama
docker compose --profile ollama up -d ollama
docker compose exec ollama ollama pull qwen3:14b
docker compose --profile ollama run --rm tradingagents-ollama
```

Verify GPU-backed inference with:

```bash
docker compose exec ollama ollama ps
nvidia-smi
```
