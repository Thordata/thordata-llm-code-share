# thordata-llm-code-share
A small, practical tool that turns a local repository into **read-only, LLM-friendly text endpoints**.

It supports two reading modes:
- **Full snapshot mode:** `/all` (FAST index) → `/all?part=N` (chunked bundles)
- **Precise mode:** `/tree` → `/file?path=...` (fetch only what’s needed)

Default is safe (localhost-only). Optional Cloudflare Quick Tunnel makes it shareable with remote LLMs.


---

## Problem
Modern LLMs often cannot “see” your repo reliably:
- repos are too large for chat context windows
- manual copy-paste is slow and error-prone
- LLM URL fetchers can time out on large payloads
- you want the model to read the repo exactly as-is (not a partial paste)

---

## Solution (how it works)
This tool provides a tiny HTTP server that exposes your repo as plain text:

### Mode 1 — Full snapshot (LLM-friendly bundles)
1) `GET /build` scans the repo and writes cached bundle files into `.llm_cache/`
2) `GET /all` returns a tiny index instantly (FAST)
3) `GET /all?part=N` returns chunk `N` as plain text, streamed from disk

Why chunking helps:
- Many LLM “URL reader” tools have strict timeouts.
- Smaller chunks reduce the risk of a single large response failing.

### Mode 2 — Precise reads
- `GET /tree` returns a filtered list of files.
- `GET /file?path=...` returns a single file as text.

This scales best for large repos: the model reads only relevant files.

---

## When to use
### Great fits
- “Read my repo and explain the architecture.”
- “Find the bug across multiple modules.”
- “Review my codebase for security/performance issues.”
- “Generate docs, API reference, or migration notes.”
- “Let an agent fetch files by URL instead of pasting.”

### Not a good fit
- Don’t use Quick Tunnel for production-grade hosting. It is for temporary sharing.
- Don’t share public URLs if your repo may contain secrets not covered by ignore rules.

---

## Quick start

### Option A — Public sharing via Cloudflare Quick Tunnel
1) Install `cloudflared` and ensure it’s in PATH.
2) Run:

```bash
python start_quick_tunnel.py --root "/path/to/your/repo" --chunk-bytes 600000 --auto-port
```

The launcher prints:
- public base URL `https://xxxx.trycloudflare.com`
- `/all` (start here), `/tree`, `/all?part=1..N`, example `/file?path=...`
- two prompt templates: Full snapshot + Precise files

If you need a proxy:
```bash
python start_quick_tunnel.py --root "/path/to/your/repo" --chunk-bytes 600000 --auto-port --proxy "http://127.0.0.1:7897"
```

### Option B — Local only
```bash
python llm_server.py --root "/path/to/your/repo" --warmup
```

---

## Endpoints
- `GET /`  
  Short help text.

- `GET /health`  
  Health check: returns `ok`.

- `GET /robots.txt`  
  `Disallow: /` to reduce crawler noise.

- `GET /tree`  
  Filtered file list in TSV: `rel_path<TAB>size_bytes`.

- `GET /file?path=...`  
  Single file as text. Enforces:
  - no path escape beyond repo root
  - blocked by ignore rules / binary detection

- `GET /build[?refresh=1]`  
  Build cached bundles and return meta JSON.

- `GET /meta`  
  Return cached meta JSON (requires prior build).

- `GET /all`  
  Return FAST index from cache (or hints to build).

- `GET /all?part=N`  
  Return chunk N (streamed).

---

## Tuning guide (timeouts vs parts)
### Key flags
- `--chunk-bytes` (bundle chunk size)
- `--max-single-file-bytes` (truncate large single files)

### Recommended values
- `600000` (600 KB): more stable, more parts
- `900000` (900 KB): balanced default
- `1200000` (1.2 MB): fewer parts, higher timeout risk for some LLM fetchers

If an LLM frequently fails to fetch parts:
- reduce `--chunk-bytes`
- consider excluding large/noisy files (lockfiles, generated files, big JSON)

---

## Security model
Safe-by-default behavior:
- binds to `127.0.0.1`
- no directory listing / no raw file serving
- blocks common secret filenames and extensions (`.env`, `.pem`, `.key`, etc.)
- ignores common dependency/build directories
- skips symlinks and binary-looking files (null bytes)

Important: this is **risk reduction**, not a guarantee. Always assume public URLs expose what passes the filters.

---

## Limitations
- Quick Tunnels have no uptime guarantees and may disconnect; keep the launcher running.
- The public hostname changes each run (Quick Tunnel behavior).
- Ignore rules are heuristic; review them for your org’s needs.
- LLM fetchers vary: some cache aggressively, some have strict timeouts.

---

## Troubleshooting

### Cloudflare error 1033 on public URL
Usually means `cloudflared` is not healthy or has exited. Keep the launcher terminal open and check cloudflared logs.

### Corporate Wi‑Fi: URL works on mobile data but not on your laptop
This usually means your corporate network blocks or interferes with `*.trycloudflare.com` (DNS poisoning, IP blocking, TLS inspection, etc.).

What to do:
- If you use Clash/other proxy tools, force `trycloudflare.com` to go through your working proxy group (not DIRECT).
- Consider enabling Clash DNS / TUN if DNS is polluted.
- Keep `--public-check warn`: local failure to open the URL does **not** necessarily mean the public URL is unreachable for your LLM.

Quick verification:
- Test on mobile 4G/5G: `https://<your>.trycloudflare.com/health`
- Or test from a different network.

### Works locally but public /health times out
Try:
- `--proxy` if your network requires it
- switch protocol (`--protocol http2` vs `--protocol quic`)
- reduce `--chunk-bytes` to avoid large responses

### Too many parts / index says dozens of bundles
Increase `--chunk-bytes` slightly, or exclude noisy folders/files.

---

## License
MIT