# ai-node local Qwen Docker deployment

This fork pins Ollama, grants only the Ollama service NVIDIA GPU access, and
keeps Qwen data in the `ollama_data` volume. TradingAgents remains an
on-demand CLI container and publishes no host port.

Create the local environment file. It holds no Alpaca credentials: production
owns every outside credential, and ai-node reads news only through production's
private research gateway. Never commit this file:

```bash
cp .env.ollama.example .env
chmod 600 .env
nano .env
```

Ticker news combines Alpaca (through production's `/alpaca-news` gateway route)
and Yahoo, removes duplicate URLs/headlines, and includes a bounded plain-text
Alpaca article-body excerpt when Alpaca supplies one. Global news and insider
transactions remain on Yahoo. When the production gateway is unreachable or
rejects the runner token, ticker news continues with Yahoo. If an old `.env`
still contains `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`, delete both lines:
nothing reads them any more.

Check the production gateway from ai-node (it uses the runner's own token file):

```bash
curl -s -H "Authorization: Bearer $(cat runner_api_token.txt)" \
  "https://thinkpad-prod.tailaf054d.ts.net/api/research/AAPL/alpaca-news?as_of=$(date +%F)&lookback_days=3&limit=2&include_content=true" |
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d['news']['provider'], len(d['news']['items']), 'articles')"
```

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
Backtests are submitted as durable batches (`POST /v1/batches`: stocks x past
dates). Their jobs run after live jobs, use an isolated empty decision memory so
a replay cannot learn from the future, survive runner restarts (an interrupted
job is retried twice), and publish a recommendation dataset that production
pulls from `GET /v1/batches/{id}/results`.
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
THINKPAD_PROD_IP=100.x.y.z
```

Set `THINKPAD_PROD_IP` to thinkpad-prod's Tailscale address, not ai-node's.
The runner maps the production MagicDNS name to this address because Docker's
embedded DNS may not resolve tailnet-only names. It reuses its runner API token
to authenticate to production's OpenBB gateway; the internal OpenBB token
never leaves production.

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

Ticker-news tools query production OpenBB first. Alpaca (via production) and
Yahoo remain ordered fallbacks when OpenBB is unavailable or has no coverage for an older
analysis date.
