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

## Private production runner

The runner queues analyses (first in, first out, up to 200 waiting) and runs
them one at a time to protect the GPU. It persists job/progress state under
the existing TradingAgents data volume and exposes only an authenticated API.
A restart fails the analysis that was running; analyses still waiting resume.
Backtest jobs (`mode: backtest`) run after live jobs and use an isolated, empty
decision memory so a historical replay cannot learn from the future.
Progress is structured like the terminal UI: team/agent status, messages and
tool calls, and each report as its stage finishes.
Bind it to ai-node's Tailscale address, never its LAN address:

```bash
cd ~/TradingAgents
openssl rand -hex 32 > runner_api_token.txt
chmod 600 runner_api_token.txt

tailscale ip -4
nano .env
```

Set the printed Tailscale address in `.env`:

```dotenv
TRADINGAGENTS_RUNNER_BIND_IP=100.x.y.z
TRADINGAGENTS_RUNNER_TOKEN_FILE=./runner_api_token.txt
```

Start the runner and its existing Ollama dependency:

```bash
docker compose --profile runner build tradingagents-runner
docker compose --profile runner up -d ollama tradingagents-runner
curl "http://$(tailscale ip -4):8000/health/ready"
```

Allow TCP 8000 only from thinkpad-prod's Tailscale IP, with the Tailscale ACL
providing an independent second boundary:

```bash
sudo ufw allow in on tailscale0 from <THINKPAD_PROD_TAILSCALE_IP> \
  to any port 8000 proto tcp comment 'TradingAgents runner from prod only'
```

Copy `runner_api_token.txt` to thinkpad-prod as
`/opt/trading/secrets/tradingagents_runner_token.txt` without printing its
contents. The production UI holds this token server-side; browsers never
receive it.
