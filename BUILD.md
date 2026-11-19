# Build guide: kodros-sec-edgar-mcp

## You need

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- A contact string for the SEC, in the form `app-name you@example.com`. The SEC rejects requests without one.
- Network access to `sec.gov` and `data.sec.gov` for live calls. The test suite runs offline.

## Build

```sh
git clone https://github.com/thefiredev-cloud/kodros-sec-edgar-mcp.git
cd kodros-sec-edgar-mcp
uv sync
```

`uv sync` reads `pyproject.toml` and `uv.lock`, creates `.venv` and installs the pinned dependencies (FastMCP, httpx, defusedxml and the test tools).

## Test

```sh
export SEC_EDGAR_USER_AGENT="my-app you@example.com"
uv run pytest
```

On 2026-11-18 this ran 49 tests in under a second. They cover the math, signal scoring, XML entity rejection and outcome grading. Two scripts in `tests/` (`smoke_live.py`, `smoke_signals.py`) call the real SEC and need network. They are not part of `pytest`.

## Run the server

```sh
uv run sec-edgar-mcp
```

The server speaks MCP over stdio, so it waits for a client. Register it in a client with this entry (change the path and contact):

```json
{ "mcpServers": { "sec-edgar": {
  "command": "uv",
  "args": ["run", "--directory", "/path/to/kodros-sec-edgar-mcp", "sec-edgar-mcp"],
  "env": { "SEC_EDGAR_USER_AGENT": "my-app you@example.com" } } } }
```

## Fill the signal store (optional)

```sh
uv run sec-edgar-collect sec --days 2       # EDGAR daily index: Form 4, 144, 8-K, 13D/G, NT, 424B, 25-NSE
uv run sec-edgar-collect prices             # daily bars and technical signals for the ticker universe
uv run sec-edgar-collect pm                 # Polymarket and Kalshi prediction markets
uv run sec-edgar-collect outcomes           # control index, 1/5/10/20 day returns, grade every kind
uv run sec-edgar-collect replay --step 5    # walk-forward replay of the price detectors on stored bars
uv run sec-edgar-collect stats              # row counts by kind and by recent date
```

`sec` reads the EDGAR daily index for the last 2 days, scores signals and writes them to SQLite. `outcomes` builds the equal-weight control from the stored bars, resolves forward returns and grades each signal kind; run it after `sec` and `prices` have written data. `all` runs `sec`, `pm`, `prices`, cluster detection and `outcomes` in one pass.

Every subcommand except `stats` needs `SEC_EDGAR_USER_AGENT` and takes an exclusive lock in the database directory, so two collectors cannot double the SEC request rate.

## Files it writes

| Path | Setting | Default |
| --- | --- | --- |
| HTTP cache | `SEC_EDGAR_CACHE` | `~/.cache/sec-edgar-mcp` |
| Signal store | `SEC_EDGAR_DB` | `~/.local/share/sec-edgar-mcp/signals.db` |

## Troubleshooting

- HTTP 403 from sec.gov: `SEC_EDGAR_USER_AGENT` is missing or has no email.
- Slow calls: the client throttles to about 8 requests per second on purpose.
- Price tools fail: they use Yahoo's unofficial chart endpoint, which can change without notice.
