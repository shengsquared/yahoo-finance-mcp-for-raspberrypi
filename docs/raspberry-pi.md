# Running on a Raspberry Pi

Nothing in this server is architecture-specific — it is pure Python, and its heavy
dependencies (`numpy`, `pandas`, `curl_cffi`) all publish prebuilt `aarch64` wheels. The
work of getting it onto a Pi is therefore mostly about three things:

1. running a **64-bit** OS, so those wheels are actually usable;
2. serving over **HTTP instead of stdio**, so a client on another machine can reach it;
3. keeping it alive across reboots with **systemd** or Docker.

## 1. Hardware and OS

| | Recommended |
|---|---|
| Board | Pi 5, Pi 4, Pi 400, Pi 3, or Pi Zero 2 W (any ARMv8 board) |
| OS | Raspberry Pi OS **64-bit** (Bookworm or Trixie), or Ubuntu Server 24.04 arm64 |
| RAM | 1 GB is comfortable; 512 MB (Pi Zero 2 W) works |
| Python | 3.11+ (Bookworm ships 3.11, Trixie ships 3.13) |

Check what you have:

```bash
uname -m        # want aarch64
python3 -V      # want 3.11 or newer
```

**If `uname -m` prints `armv7l` or `armv6l`** you are on a 32-bit userland. PyPI publishes
no `numpy`/`pandas` wheels for 32-bit ARM, so `pip` falls back to compiling them — which on
a Pi means an hour or more of build time, a full toolchain, and usually extra swap.
Raspberry Pi OS points pip at [piwheels](https://www.piwheels.org), which does host 32-bit
ARM builds and avoids the compile, but the versions available lag behind. Reflashing with
the 64-bit image is much less work than fighting this. Original Pi 1 / Pi Zero (ARMv6)
boards are not practical targets.

**If `python3 -V` prints 3.9** you are on Bullseye or older. Either upgrade the OS, or
install a standalone Python with `uv` (below), which downloads a prebuilt `aarch64` runtime
and does not touch the system Python.

## 2. Pick a transport

The upstream server only spoke **stdio**, where the MCP client launches the server as a
child process and talks to it over pipes. That works if the client runs on the Pi itself,
but the usual reason to put this on a Pi is to have it running all the time and connect
from a laptop. So the server now also speaks HTTP:

```bash
yahoo-finance-mcp                              # stdio (default, unchanged)
yahoo-finance-mcp --transport streamable-http  # HTTP on 127.0.0.1:8000/mcp
yahoo-finance-mcp --transport sse              # older SSE transport, on /sse
```

Every flag has an environment variable equivalent, which is what the systemd unit and the
Docker image use:

| Flag | Env var | Default |
|---|---|---|
| `--transport` | `YFINANCE_MCP_TRANSPORT` | `stdio` |
| `--host` | `YFINANCE_MCP_HOST` | `127.0.0.1` |
| `--port` | `YFINANCE_MCP_PORT` | `8000` |
| `--cache-dir` | `YFINANCE_CACHE_DIR` | `~/.cache/yahoo-finance-mcp` |
| `--log-level` | `YFINANCE_MCP_LOG_LEVEL` | `INFO` |

Use `--host 0.0.0.0` to accept connections from the rest of the LAN. Read the security
note in section 6 before you do.

## 3. Install

### Option A — try it out with `uvx`

No clone, no virtualenv. `uv` also fetches its own Python if the system one is too old:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uvx --from git+https://github.com/shengsquared/yahoo-finance-mcp-for-raspberrypi \
    yahoo-finance-mcp --transport streamable-http --host 0.0.0.0
```

The first run downloads and caches the dependencies (~60 MB); later runs start in seconds.

### Option B — install as a systemd service (recommended)

```bash
git clone https://github.com/shengsquared/yahoo-finance-mcp-for-raspberrypi.git
cd yahoo-finance-mcp-for-raspberrypi
sudo bash scripts/install-pi.sh
```

The script creates a virtualenv in `/opt/yahoo-finance-mcp`, installs the server into it,
and enables [`deploy/yahoo-finance-mcp.service`](../deploy/yahoo-finance-mcp.service),
which starts on boot and restarts on failure. Override the defaults if you want:

```bash
sudo MCP_PORT=9000 MCP_HOST=127.0.0.1 bash scripts/install-pi.sh
```

Afterwards:

```bash
systemctl status yahoo-finance-mcp
journalctl -u yahoo-finance-mcp -f
sudo systemctl restart yahoo-finance-mcp
```

To change settings later, edit `/etc/systemd/system/yahoo-finance-mcp.service` (or
`sudo systemctl edit yahoo-finance-mcp`), then
`sudo systemctl daemon-reload && sudo systemctl restart yahoo-finance-mcp`.

The unit runs under `DynamicUser=yes` with a read-only filesystem and a `MemoryMax=512M`
cap, and keeps its yfinance timezone cache in `/var/lib/yahoo-finance-mcp`.

### Option C — Docker

The `Dockerfile` builds from `python:3.11-slim-bookworm`, which is published for arm64, so
it builds natively on a 64-bit Pi:

```bash
docker compose up -d --build
```

Building on the Pi takes a few minutes. To build on a faster machine instead:

```bash
docker buildx build --platform linux/arm64 -t yahoo-finance-mcp .
```

The image defaults to `streamable-http` on `0.0.0.0:8000`. For a stdio client:

```bash
docker run -i --rm yahoo-finance-mcp yahoo-finance-mcp --transport stdio
```

## 4. Connect a client

Find the Pi's address first — `hostname -I`, or use its mDNS name (`raspberrypi.local`).

**Claude Code**, from your laptop:

```bash
claude mcp add --transport http yfinance http://raspberrypi.local:8000/mcp
```

**Claude Desktop** launches MCP servers as local processes, so bridge the HTTP endpoint
back to stdio with `mcp-remote` in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "yfinance": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://raspberrypi.local:8000/mcp", "--allow-http"]
    }
  }
}
```

**A client running on the Pi itself** can skip the network entirely and use stdio:

```json
{
  "mcpServers": {
    "yfinance": {
      "command": "/opt/yahoo-finance-mcp/.venv/bin/yahoo-finance-mcp"
    }
  }
}
```

Quick check that the service is up from another machine:

```bash
curl -i -X POST http://raspberrypi.local:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

## 5. Resource notes

- Importing `pandas` costs roughly 100–150 MB of RSS, and a large options chain or `max`
  period history briefly adds more. That is the whole reason for the `MemoryMax=512M` cap —
  it bounds the damage rather than reserving anything.
- Cold start on a Pi Zero 2 W is a few seconds, mostly `pandas` import. Pi 4/5 is fast.
- Yahoo rate-limits aggressively. A Pi that is always on makes it easy to hammer the API
  from a loop; if you hit "Too Many Requests", back off rather than retrying.
- SD cards wear out. If you are running this permanently, an SSD or USB boot is kinder,
  and the cache directory is deliberately small.

## 6. Security

**This server has no authentication.** Anything that can reach the port can call every
tool. On the HTTP transports:

- Bind to `127.0.0.1` unless you actually need remote access.
- If you need remote access, keep it on the LAN or a private overlay network such as
  Tailscale or WireGuard. `YFINANCE_MCP_HOST=0.0.0.0` plus a port-forward on your router
  puts an unauthenticated service on the public internet — don't.
- If you must expose it beyond the LAN, put a reverse proxy (Caddy, nginx) in front with
  TLS and authentication, and leave the server bound to localhost behind it.

The data it serves is public market data, but the Pi's outbound bandwidth and Yahoo's rate
limits are yours to lose.

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `pip` spends forever "Building wheel for numpy" | 32-bit userland, or a Python version with no wheels yet. See section 1. |
| `ERROR: Could not find a version that satisfies the requirement mcp` | Python older than 3.11. Check `python3 -V`; use `uv` or upgrade the OS. |
| `error: externally-managed-environment` | Bookworm's pip refuses to install into the system Python. Use a virtualenv — which is what `install-pi.sh` does. |
| Service starts then exits, `journalctl` shows a permissions error on a cache path | Point `YFINANCE_CACHE_DIR` at a writable directory; under systemd that is `/var/lib/yahoo-finance-mcp`. |
| Client connects but immediately disconnects on stdio | Something is printing to stdout. The server logs to stderr for exactly this reason; a wrapper script that echoes will break the JSON-RPC stream. |
| `Connection refused` from another machine | Server is bound to `127.0.0.1`. Set `YFINANCE_MCP_HOST=0.0.0.0` and restart. |
| Tools return "Too Many Requests" | Yahoo rate limiting. Wait it out; it is not a Pi-specific problem. |
| `raspberrypi.local` doesn't resolve | mDNS is unavailable on your network — use the IP from `hostname -I`. |
