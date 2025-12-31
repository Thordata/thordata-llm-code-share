#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path
from queue import Queue, Empty
import threading
import webbrowser

TRY_HOST_RE = re.compile(r"https://([a-z0-9-]+)\.trycloudflare\.com\b", re.I)
QUICK_TUNNEL_FAIL_RE = re.compile(r"failed to request quick tunnel", re.I)


def safe_console_write(line: str) -> None:
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.buffer.write(line.encode(enc, errors="replace"))
        sys.stdout.flush()


def http_get(url: str, timeout: float = 5.0, proxy: str = "") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "thordata-llm-code-share/1.0"})
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # disable system proxy
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def wait_http_ok(url: str, timeout_sec: float, proxy: str = "") -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        try:
            _ = http_get(url, timeout=3.0, proxy=proxy)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def port_is_free(bind: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.bind((bind, port))
            return True
        except OSError:
            return False


def pick_free_port(bind: str, start_port: int, max_tries: int = 200) -> int:
    p = start_port
    for _ in range(max_tries):
        if port_is_free(bind, p):
            return p
        p += 1
    raise RuntimeError(f"no free port found from {start_port}")


def is_valid_quick_host(host: str) -> bool:
    h = host.lower().strip()
    if h in {"api"}:
        return False
    if "-" not in h:
        return False
    return True


def pump(proc: subprocess.Popen, prefix: str, url_q: Queue | None, err_q: Queue | None, keep: deque) -> None:
    try:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            keep.append(line.rstrip("\n"))
            safe_console_write(prefix + line)

            if err_q is not None and QUICK_TUNNEL_FAIL_RE.search(line):
                err_q.put(line.strip())

            if url_q is not None:
                m = TRY_HOST_RE.search(line)
                if m and is_valid_quick_host(m.group(1)):
                    url_q.put(f"https://{m.group(1)}.trycloudflare.com")
    except Exception as e:
        safe_console_write(prefix + f"[WARN] pump stopped: {e}\n")


def format_llm_index(public_url: str, repos: list[str]) -> str:
    lines = []
    lines.append("下面有多个仓库快照（同一个只读文本服务，不同仓库用路径区分）。请按需阅读后回答问题。")
    lines.append("")
    lines.append("通用阅读规则：")
    lines.append("1）先打开目标仓库的 /all（索引），看有多少 part。")
    lines.append("2）需要全量就按顺序读 /all?part=1..N；需要精准就用 /tree + /file。")
    lines.append("3）引用代码时带上 FILE: ... 路径。")
    lines.append("")
    lines.append("仓库入口：")
    for r in repos:
        base = f"{public_url}/r/{r}"
        lines.append(f"- {r}")
        lines.append(f"  - Index: {base}/all")
        lines.append(f"  - Tree:  {base}/tree")
        lines.append(f"  - File:  {base}/file?path=README.md")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", action="append", required=True, help='repeatable: "name=/path" or "/path"')
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--auto-port", action="store_true", help="if port is busy, auto pick next free port")

    ap.add_argument("--chunk-bytes", type=int, default=600000)
    ap.add_argument("--max-single-file-bytes", type=int, default=3000000)
    ap.add_argument("--cache-dirname", default=".llm_cache")

    ap.add_argument("--cloudflared", default="cloudflared")
    ap.add_argument("--protocol", default="http2", choices=["http2", "quic"])
    ap.add_argument("--proxy", default="", help='optional http proxy for cloudflared, e.g. "http://127.0.0.1:7897"')

    ap.add_argument("--public-check", default="warn", choices=["warn", "strict", "off"])
    ap.add_argument("--wait-public-seconds", type=float, default=30.0)

    ap.add_argument("--warmup", action="store_true")
    ap.add_argument("--auto-build", action="store_true")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--server-script", default=None, help="path to llm_multi_server.py (default: same dir)")
    args = ap.parse_args()

    # pick port
    if not port_is_free(args.bind, args.port):
        if args.auto_port:
            new_port = pick_free_port(args.bind, args.port)
            safe_console_write(f"[WARN] port {args.port} busy, picked free port: {new_port}\n")
            args.port = new_port
        else:
            safe_console_write(f"[FATAL] port {args.port} is busy. Use --auto-port or choose another --port\n")
            sys.exit(2)

    this_dir = Path(__file__).resolve().parent
    server_script = Path(args.server_script).resolve() if args.server_script else (this_dir / "llm_multi_server.py")
    if not server_script.exists():
        print(f"[FATAL] llm_multi_server.py not found: {server_script}")
        sys.exit(2)

    local_base = f"http://{args.bind}:{args.port}"

    # Start multi server
    server_cmd = [
        sys.executable, str(server_script),
        "--bind", args.bind,
        "--port", str(args.port),
        "--cache-dirname", args.cache_dirname,
        "--chunk-bytes", str(args.chunk_bytes),
        "--max-single-file-bytes", str(args.max_single_file_bytes),
    ]
    if args.warmup:
        server_cmd.append("--warmup")
    if args.auto_build:
        server_cmd.append("--auto-build")
    for r in args.repo:
        server_cmd += ["--repo", r]

    safe_console_write("[1/3] Starting multi-repo server:\n")
    safe_console_write("  " + " ".join(server_cmd) + "\n")

    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"

    server_keep = deque(maxlen=200)
    server_proc = subprocess.Popen(
        server_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        bufsize=1,
    )
    threading.Thread(target=pump, args=(server_proc, "[server] ", None, None, server_keep), daemon=True).start()

    if not wait_http_ok(local_base + "/health", timeout_sec=25, proxy=""):
        safe_console_write("[FATAL] multi server not ready.\n")
        if server_proc.poll() is not None:
            safe_console_write(f"[FATAL] server process already exited with code {server_proc.returncode}\n")
        safe_console_write("---- server last lines ----\n")
        for line in list(server_keep)[-120:]:
            safe_console_write(line + "\n")
        sys.exit(3)

    # Start ONE cloudflared
    safe_console_write("[2/3] Starting cloudflared quick tunnel:\n")
    cf_cmd = [args.cloudflared, "tunnel", "--protocol", args.protocol, "--url", local_base]
    safe_console_write("  " + " ".join(cf_cmd) + "\n")

    cf_env = os.environ.copy()
    if args.proxy:
        cf_env["http_proxy"] = args.proxy
        cf_env["https_proxy"] = args.proxy
        cf_env["HTTP_PROXY"] = args.proxy
        cf_env["HTTPS_PROXY"] = args.proxy
        cf_env["NO_PROXY"] = "127.0.0.1,localhost"

    cf_keep = deque(maxlen=400)
    url_q: Queue[str] = Queue()
    err_q: Queue[str] = Queue()

    cf_proc = subprocess.Popen(
        cf_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=cf_env,
        bufsize=1,
    )
    threading.Thread(target=pump, args=(cf_proc, "[cf] ", url_q, err_q, cf_keep), daemon=True).start()

    public_url = None
    fail_line = None
    t0 = time.time()
    while time.time() - t0 < 60:
        if cf_proc.poll() is not None:
            break
        try:
            fail_line = err_q.get_nowait()
            break
        except Empty:
            pass
        try:
            public_url = url_q.get_nowait()
            break
        except Empty:
            pass
        time.sleep(0.05)

    if fail_line:
        print("[FATAL] cloudflared failed to request quick tunnel:")
        print(" ", fail_line)
        if not args.proxy:
            print('Hint: try with --proxy "http://127.0.0.1:7897" (if you use Clash).')
        sys.exit(4)

    if not public_url:
        print("[FATAL] could not obtain trycloudflare url.")
        print("---- cloudflared last lines ----")
        for line in list(cf_keep)[-120:]:
            print(line)
        sys.exit(4)

    safe_console_write(f"[3/3] Public base: {public_url}\n")

    if args.public_check != "off":
        ok = wait_http_ok(public_url + "/health", timeout_sec=float(args.wait_public_seconds), proxy=(args.proxy or ""))
        if not ok:
            print(f"[WARN] public /health not OK within {args.wait_public_seconds}s (mode={args.public_check}).")
            if args.public_check == "strict":
                print("[FATAL] strict mode exit.")
                sys.exit(5)

    # repo names
    names = []
    seen = {}
    for s in args.repo:
        if "=" in s:
            name = s.split("=", 1)[0].strip()
        else:
            name = os.path.basename(s.strip().rstrip("/\\"))
        base = name
        i = seen.get(base, 0)
        if i == 0:
            seen[base] = 1
            names.append(base)
        else:
            seen[base] = i + 1
            names.append(f"{base}-{i+1}")

    print("\n============================================================")
    print("[READY] Multi-repo links (single Quick Tunnel):")
    print(f"Home/Repos: {public_url}/repos")
    for r in names:
        base = f"{public_url}/r/{r}"
        print(f"\n- {r}")
        print(f"  Index: {base}/all")
        print(f"  Tree:  {base}/tree")
        print(f"  File:  {base}/file?path=README.md")
    print("============================================================\n")

    print("[Copy to LLM]\n------------------------------------------------------------")
    print(format_llm_index(public_url, names))
    print("------------------------------------------------------------\n")

    if args.open:
        try:
            webbrowser.open(public_url + "/repos", new=2)
        except Exception:
            pass

    print("Stop: press Ctrl+C in this terminal.\n")
    try:
        while True:
            time.sleep(1)
            if server_proc.poll() is not None:
                print("[FATAL] server exited unexpectedly")
                break
            if cf_proc.poll() is not None:
                print("[FATAL] cloudflared exited unexpectedly")
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            cf_proc.terminate()
        except Exception:
            pass
        try:
            server_proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()