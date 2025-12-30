# thordata-llm-code-share

Expose a local repository as LLM-friendly text endpoints:
- `/all` -> fast index
- `/all?part=N` -> fast chunk fetch
- `/tree` -> filtered file list
- `/file?path=...` -> read single file

## Security
This tool blocks common secrets by default:
- `.env`, `.pem`, `.key`, `.p12`, `.pfx`, etc.

Still: **Do not keep real secrets inside your repo** before sharing any tunnel URL.

## Requirements
- Python 3.10+
- (optional) cloudflared for sharing a public URL

## Usage (Windows Git Bash)

### 1) Start server (warmup recommended)
Example: share `D:\Thordata_Work\thordata-python-sdk`

```bash
python /d/path/to/thordata-llm-code-share/llm_server.py \
  --root "/d/Thordata_Work/thordata-python-sdk" \
  --warmup \
  --port 8080
```

### 2) Local check
Open:
- http://127.0.0.1:8080/all
- http://127.0.0.1:8080/all?part=1
- http://127.0.0.1:8080/tree

### 3) Expose with Cloudflare Quick Tunnel
```bash
cloudflared tunnel --protocol http2 --url http://127.0.0.1:8080
```

Cloudflare will print a `https://xxxx.trycloudflare.com` URL.
Share:
- https://xxxx.trycloudflare.com/all
- https://xxxx.trycloudflare.com/all?part=1
- https://xxxx.trycloudflare.com/all?part=2
...