# thordata-llm-code-share

Expose a local code repository as **LLM-friendly, read-only text endpoints** — either locally (safe by default) or through a **Cloudflare Quick Tunnel** for sharing with remote LLMs.

This tool is designed for a practical workflow:

1. Start a local server that can list files and serve files as plain text (with safety filters).
2. Optionally build a cached, chunked “bundle” so LLM fetchers can read the whole repo reliably.
3. (Optional) Start a Cloudflare Quick Tunnel and share a public URL with your LLM.
4. Give the LLM a prompt template so it reads `/all` (index) → `/all?part=N` (chunks), or uses `/tree` + `/file` for precise reads.

---

## What’s included

- `llm_server.py`: read-only HTTP server exposing the repo as text endpoints
- `start_quick_tunnel.py`: launcher that starts the server + `cloudflared` Quick Tunnel and prints shareable URLs and prompt templates

---

## Key features

### LLM-friendly “whole repo” reading
- `/build` generates chunked bundle files into a cache directory (default: `.llm_cache/`)
- `/all` returns a **FAST index** (seconds)
- `/all?part=N` returns a specific chunk (fast file streaming)

### Precise file reading (recommended for large repos)
- `/tree` returns a filtered file list with sizes
- `/file?path=...` returns a single file (plain text) with a header

### Safe-by-default
- Default bind: `127.0.0.1` (localhost only)
- No directory listing; only the endpoints below
- Blocks common secrets and binary formats (`.env`, `.pem`, `.key`, etc.)
- Skips dependency/build output folders (`node_modules`, `dist`, `target`, `vendor`, etc.)
- Skips binary-looking files (null bytes)
- Truncates very large single files to avoid huge chunks

---

## Requirements

- Python 3 (recommended: 3.9+)
- Optional: `cloudflared` (only needed if you want a public share link)

---

## Quick start (local only)

From the repo root of **thordata-llm-code-share**:

```bash
python llm_server.py --root "/path/to/your/repo" --warmup
```

Then open:

- `http://127.0.0.1:8080/health`
- `http://127.0.0.1:8080/all` (index)
- `http://127.0.0.1:8080/all?part=1` (chunk 1)
- `http://127.0.0.1:8080/tree` (file list)
- `http://127.0.0.1:8080/file?path=README.md` (single file)

---

## Quick start (public share via Cloudflare Quick Tunnel)

> This starts both `llm_server.py` and `cloudflared`, then prints public URLs + prompt templates.

```bash
python start_quick_tunnel.py --root "/path/to/your/repo" --chunk-bytes 600000 --auto-port
```

What you’ll get in the terminal:
- Public base URL: `https://xxxx.trycloudflare.com`
- `.../all`, `.../tree`, `.../all?part=1..N`
- Prompt templates (Full snapshot + Precise files)

### If your network requires an HTTP proxy (example: Clash)
```bash
python start_quick_tunnel.py \
  --root "/path/to/your/repo" \
  --chunk-bytes 600000 \
  --auto-port \
  --proxy "http://127.0.0.1:7897"
```

This passes proxy env vars to `cloudflared`.

---

## Endpoints

### `GET /`
Shows a short help text.

### `GET /health`
Returns `ok` if server is alive.

### `GET /robots.txt`
Returns `Disallow: /` to reduce crawler noise.

### `GET /tree`
Returns a tab-separated file list:

- `rel_path<TAB>size_bytes`

Files are filtered by ignore rules.

### `GET /file?path=relative/path/to/file.py`
Returns a single file as text (with a header). Security checks:
- must stay under `--root`
- blocked if secret/binary/ignored
- symlinks skipped

### `GET /build[?refresh=1]`
Builds `.llm_cache/` bundles and returns `meta.json` as JSON.

### `GET /meta`
Returns the cached `meta.json` (requires cache to exist).

### `GET /all`
If cache exists: returns `index.txt` (FAST).
If not: suggests running `/build`.

### `GET /all?part=N`
Returns bundle `N` (fast streaming from cached bundle files).

---

## CLI usage

### `llm_server.py`
Common flags:

- `--root`: repo root (default: current directory)
- `--bind`: bind address (default: `127.0.0.1`)
- `--port`: port (default: `8080`)
- `--warmup`: build cache at startup (recommended)
- `--auto-build`: auto-build cache on first `/all` if missing
- `--chunk-bytes`: chunk size in bytes (recommended: 600k–1.2MB)
- `--max-single-file-bytes`: truncate large files (default: 3MB)
- `--cache-dirname`: cache folder name under root (default: `.llm_cache`)
- `--no-lock-ignore`: include `*.lock` files (off by default)
- `--exclude-github`: exclude `.github` directory

### `start_quick_tunnel.py`
Common flags:

- `--root`: repo root (required)
- `--auto-port`: if port is busy, pick next free port
- `--chunk-bytes`: recommend `600000` for stability
- `--protocol`: `http2` or `quic` (default: `http2`)
- `--proxy`: optional proxy for cloudflared (`http://127.0.0.1:7897`)
- `--open`: open the public `/all` in your browser
- `--no-warmup`: skip warmup build (not recommended)
- `--auto-build`: server auto-builds on first `/all` if missing

---

## Recommended LLM reading workflow

### Option A — Full snapshot (simple)
1. Read `.../all` (index)
2. Read `.../all?part=1..N` in order
3. Ask questions and cite paths shown as `FILE: ...`

### Option B — Precise files (scales better)
1. Read `.../tree`
2. Fetch only what you need via `.../file?path=...`
3. Use `/all` only if you need broad context

---

## Troubleshooting

### Public URL shows Cloudflare “1033”
This usually means `cloudflared` is not healthy or exited. Keep the launcher terminal running and ensure `cloudflared` logs are still updating.

### Server is OK locally but public `/health` is not reachable
Try:
- rerun with `--proxy` if your network requires it
- switch `--protocol http2` vs `--protocol quic` depending on network restrictions
- ensure firewalls do not block outbound connections for `cloudflared`

### Many parts / slow LLM fetch
Tune `--chunk-bytes`:
- smaller chunks (e.g., 600000): more stable, more parts
- larger chunks (e.g., 1200000): fewer parts, may time out for some fetchers

---

## License
MIT