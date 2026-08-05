# MCP Network Capture

## Playwright MCP + Filesystem MCP + Python Client

Capture real browser network requests (URLs, methods, status, headers, optional bodies/traces) and save them locally as **newline-delimited JSON (JSONL)**.
This repo wires **Playwright MCP** (browser automation) with a **Filesystem MCP** (safe writes) and a **small headless Python client**.

---

## Why this exists

* You needed **reliable network capture** from a real browser session.
* `browser-use` doesn’t expose network capture as a first-class tool; Playwright MCP does.
* This solution is **minimal**, **scriptable**, and **production-ready**: navigate → wait correctly → fetch network logs → persist JSONL.

---

## What you get

* ✅ **Headless CLI** (`capture_network.py`) – automate captures without a chat UI
* ✅ **Semantic waits** (`networkidle`) with fallback to timed sleep
* ✅ **Retries** with exponential backoff for flaky pages/tools
* ✅ **Client-side filters** (URL regex, method, status range)
* ✅ **Structured logging** with env overrides
* ✅ **Safe local writes** via a Filesystem MCP server
* 🧩 Optional: **Interactive** agent flow with an Ollama MCP client (for ad-hoc sessions)

---

## Architecture (at a glance)

```text
+------------------+          SSE (HTTP)           +-----------------------+
|  capture_network |  -------------------------->  |  Playwright MCP       |
|  (Python client) |                                |  (real Chromium)      |
+---------+--------+                                |  tools:               |
          | stdio                                    |   - browser_navigate  |
          v                                          |   - browser_wait_for  |
+-----------------------+                            |   - browser_network_* |
| Filesystem MCP server | <------------------------  +-----------+-----------+
| tools: write_file,    |        JSONL content                  |
|        create_directory                                   Network stack
+-----------------------+                              (requests/responses)
```

---

## Requirements

* **Node.js 18+** (to run the MCP servers)
* **Python 3.10+** (tested with 3.11/3.12)
* **Playwright browsers** (Playwright MCP will prompt to install; or run `npx playwright install`)
* Optional (interactive workflow): **Ollama** and the `ollmcp` client

---

## Install

```bash
# Python dependency (choose one)
uv pip install -r requirements.txt  # recommended
# or
pip install -r requirements.txt

# No repo-level install is needed for MCP servers (run via npx below)
```

---

## Quick start

### 1) Start the Playwright MCP server (SSE)

```bash
npx @playwright/mcp@latest \
  --port 8931 \
  --block-service-workers \
  --image-responses=omit \
  --allowed-origins "https://yourapp.com;https://api.yourapp.com"
```

Tips:

* `--block-service-workers` prevents SWs from hijacking network.
* `--image-responses=omit` keeps captures small.
* Use `--save-trace` if you want a Playwright trace alongside your JSONL.

### 2) Start a Filesystem MCP server (stdio)

```bash
npx @agent-infra/mcp-server-filesystem@latest \
  --allowed-directories "$HOME/mcp_captures"
```

> Any maintained Filesystem MCP works as long as it exposes `write_file` (and, optionally, `create_directory`).

### 3) Run the capture

```bash
mkdir -p "$HOME/mcp_captures/captures"

uv run capture_network.py \
  --url https://yourapp.com \
  --out "$HOME/mcp_captures/captures/yourapp_{ts}.jsonl" \
  --sse http://127.0.0.1:8931/sse \
  --fs-cmd npx \
  --fs-args "@agent-infra/mcp-server-filesystem@latest" \
  --wait-mode networkidle \
  --wait 15 \
  --filter-url "api\\.yourapp\\.com" \
  --filter-method GET \
  --status-min 200 --status-max 399
```

Output:

```text
/Users/you/mcp_captures/captures/yourapp_20250926_140321.jsonl
```

---

## CLI options (high-value flags)

```text
--url               URL to navigate (required)
--sse               Playwright MCP SSE URL (default: http://127.0.0.1:8931/sse)
--fs-cmd            Filesystem MCP command (default: npx)
--fs-args           Filesystem MCP args (default: @agent-infra/mcp-server-filesystem@latest)
--out               Output JSONL path (supports {ts})
--wait              Seconds (timeout for semantic waiting; duration for sleep)
--wait-mode         networkidle | sleep   (default: networkidle)
--filter-url        Python regex to filter request URLs
--filter-method     GET|POST|PUT|PATCH|DELETE...
--status-min        Minimum status code to keep (default: 0)
--status-max        Maximum status code to keep (default: 999)
```

---

## Environment variables (optional)

These mirror CLI flags; set once for repeatable runs.

```text
PLAYWRIGHT_MCP_SSE_URL     # default: http://127.0.0.1:8931/sse
FILESYSTEM_MCP_CMD         # default: npx
FILESYSTEM_MCP_ARGS        # default: "@agent-infra/mcp-server-filesystem@latest"
CAPTURE_OUT                # default: ~/mcp_captures/captures/capture_{ts}.jsonl
CAPTURE_WAIT_MODE          # default: networkidle
CAPTURE_WAIT_SECS          # default: 5
CAPTURE_FILTER_URL
CAPTURE_FILTER_METHOD
CAPTURE_STATUS_MIN         # default: 0
CAPTURE_STATUS_MAX         # default: 999
LOG_LEVEL                  # default: INFO (DEBUG for dev)
LOG_FORMAT                 # default: "%(asctime)s %(levelname)s %(name)s - %(message)s"
LOG_DATEFMT                # default: "%Y-%m-%d %H:%M:%S"
```

---

## What gets written (JSONL format)

One JSON object per line; schema depends on server version. Most builds return a list of request/response pairs like:

```json
{
  "request": {
    "url": "https://api.yourapp.com/v1/users",
    "method": "GET",
    "headers": { "...": "..." },
    "postData": null
  },
  "response": {
    "status": 200,
    "headers": { "...": "..." },
    "timing": { "startTime": 123.4, "requestTime": 456.7 }
  },
  "ts": "2025-09-26T14:03:21Z"
}
```

### Peek / filter with `jq`

```bash
# Count lines
wc -l yourapp_*.jsonl

# Show all non-2xx
jq -r 'select(.response.status < 200 or .response.status >= 300) | .request.url' yourapp_*.jsonl
```

---

## Interactive (optional): drive via Ollama + `ollmcp`

If you want a chat UI to orchestrate tools:

1. Start the same two MCP servers (SSE + stdio).
2. Install the client: `pip install ollmcp`
3. Create `servers.json`:

```json
{
  "mcpServers": {
    "playwright": { "type": "sse", "url": "http://127.0.0.1:8931/sse" },
    "filesystem": { "command": "npx", "args": ["@agent-infra/mcp-server-filesystem@latest"] }
  }
}
```

4. Run:

```bash
ollmcp --servers-json ./servers.json --model qwen2.5:7b
```

5. Prompt:

> Navigate to `https://yourapp.com`, wait for network idle, call `playwright:browser_network_requests`, then `filesystem:write_file` to `captures/yourapp_{today}.jsonl`.

---

## Troubleshooting

* **Empty captures**
  Use `--wait-mode networkidle --wait 15` (or more). Some apps keep polling; consider capturing in steps (navigate → interact → capture again).

* **Navigation never settles**
  Keep `--block-service-workers` on the server. Try a longer `--wait` or change to `--wait-mode sleep`.

* **`write_file` fails**
  Your FS server likely restricts paths. Start it with `--allowed-directories "$HOME/mcp_captures"` (or equivalent).

* **Huge files**
  Add `--filter-url`, `--filter-method`, `--status-min/max`, and use the server flag `--image-responses=omit`.

* **Need response bodies/trace**
  Start Playwright MCP with `--save-trace` (inspect later with Playwright tools). If your MCP variant exposes response bodies, they’ll appear in the JSON; otherwise keep using traces.

---

## Security & compliance

* Only browse **trusted origins**; set `--allowed-origins` accordingly.
* Captured data may include **tokens, PII, headers**. Store securely, encrypt at rest, and scrub before sharing.
* Respect **robots.txt**, site Terms, and applicable laws. You are responsible for how you use this tool.

---

## Extending the solution

* **Schedule**: cron, GitHub Actions, or Airflow job invoking `capture_network.py`.
* **Storage**: swap Filesystem MCP for a cloud-storage MCP (S3/GCS) or write to both.
* **Richer waits**: add selector/text waits via `browser_wait_for` variants.
* **Multi-step flows**: navigate → click → capture → fill → capture (call script multiple times or extend it to sequence steps).
* **Metrics**: wrap runs with your telemetry (duration, request count, failure rate).

---

## Project layout (suggested)

```text
.
├─ capture_network.py           # CLI client (this README documents it)
├─ README.md
└─ examples/
   ├─ servers.json              # for ollmcp (optional)
   └─ scripts/                  # any helper shell scripts
```

---

## License

Choose a permissive license (e.g., MIT or Apache-2.0) and add it to the repo.

---

### Final notes

This setup is intentionally small and explicit. If you later outgrow JSONL or need deep protocol-level data (WebSocket frames, bodies at scale, HAR diffs), Playwright’s native **trace/HAR** features integrate cleanly with the same MCP server.
