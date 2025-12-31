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
from pathlib import Path
from queue import Queue, Empty
from collections import deque
import threading

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
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def wait_http_ok(url: str, timeout_sec: float, proxy: str = "") -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        try:
            _ = http_get(url, timeout=3.0, proxy=proxy)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def port_is_free(bind: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.bind((bind, port))
            return True
        except OSError:
            return False


def pick_next_free_port(bind: str, start_port: int, max_tries: int = 200) -> int:
    p = start_port
    for _ in range(max_tries):
        if port_is_free(bind, p):
            return p
        p += 1
    raise RuntimeError(f"No free port found from {start_port}")


def is_valid_quick_tunnel_host(host: str) -> bool:
    h = host.lower().strip()
    if h in {"api"}:
        return False
    if "-" not in h:
        return False
    return True


def pump_output(proc: subprocess.Popen, prefix: str, url_q: Queue | None, err_q: Queue | None, keep_last: deque) -> None:
    try:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            keep_last.append(line.rstrip("\n"))
            safe_console_write(f"{prefix}{line}")
            if err_q is not None and QUICK_TUNNEL_FAIL_RE.search(line):
                err_q.put(line.strip())
            if url_q is not None:
                m = TRY_HOST_RE.search(line)
                if m:
                    host = m.group(1)
                    if is_valid_quick_tunnel_host(host):
                        url_q.put(f"https://{host}.trycloudflare.com")
    except Exception as e:
        safe_console_write(f"{prefix}[WARN] pump stopped: {e}\n")


class Instance:
    def __init__(self, name: str, root: str, port: int):
        self.name = name
        self.root = root
        self.port = port
        self.local_base = f"http://127.0.0.1:{port}"
        self.public_url: str | None = None

        self.server_proc: subprocess.Popen | None = None
        self.cf_proc: subprocess.Popen | None = None

        self.server_keep = deque(maxlen=200)
        self.cf_keep = deque(maxlen=400)

        self.url_q: Queue[str] = Queue()
        self.err_q: Queue[str] = Queue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", required=True, help="repeatable: repo root path")
    ap.add_argument("--base-port", type=int, default=8080)
    ap.add_argument("--bind", default="127.0.0.1")

    ap.add_argument("--chunk-bytes", type=int, default=600000)
    ap.add_argument("--max-single-file-bytes", type=int, default=3000000)
    ap.add_argument("--cache-dirname", default=".llm_cache")

    ap.add_argument("--cloudflared", default="cloudflared")
    ap.add_argument("--protocol", default="http2", choices=["http2", "quic"])
    ap.add_argument("--proxy", default="", help="optional proxy for cloudflared, e.g. http://127.0.0.1:7897")

    ap.add_argument("--server-script", default=None)
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--auto-build", action="store_true")

    ap.add_argument("--public-check", default="warn", choices=["warn", "strict", "off"])
    ap.add_argument("--wait-public-seconds", type=float, default=30.0)

    args = ap.parse_args()

    this_dir = Path(__file__).resolve().parent
    server_script = Path(args.server_script).resolve() if args.server_script else (this_dir / "llm_server.py")
    if not server_script.exists():
        print(f"[FATAL] llm_server.py not found: {server_script}")
        sys.exit(2)

    # Child env (utf-8)
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"

    cf_env = os.environ.copy()
    if args.proxy:
        cf_env["http_proxy"] = args.proxy
        cf_env["https_proxy"] = args.proxy
        cf_env["HTTP_PROXY"] = args.proxy
        cf_env["HTTPS_PROXY"] = args.proxy
        cf_env["NO_PROXY"] = "127.0.0.1,localhost"

    # Create instances with ports
    instances: list[Instance] = []
    next_port = args.base_port
    for r in args.root:
        root = os.path.abspath(r)
        if not os.path.isdir(root):
            print(f"[SKIP] root not found: {root}")
            continue
        port = pick_next_free_port(args.bind, next_port)
        next_port = port + 1
        name = os.path.basename(root.rstrip("/\\"))
        instances.append(Instance(name=name, root=root, port=port))

    if not instances:
        print("[FATAL] no valid roots")
        sys.exit(2)

    safe_console_write("============================================================\n")
    safe_console_write("thordata-llm-code-share: Multi Quick Tunnel Launcher\n")
    for inst in instances:
        safe_console_write(f"- {inst.name}\n  ROOT:  {inst.root}\n  LOCAL: {inst.local_base}\n")
    safe_console_write("============================================================\n\n")

    # Start servers first
    for inst in instances:
        server_cmd = [
            sys.executable, str(server_script),
            "--root", inst.root,
            "--bind", args.bind,
            "--port", str(inst.port),
            "--cache-dirname", args.cache_dirname,
            "--chunk-bytes", str(args.chunk_bytes),
            "--max-single-file-bytes", str(args.max_single_file_bytes),
        ]
        if not args.no_warmup:
            server_cmd.append("--warmup")
        if args.auto_build:
            server_cmd.append("--auto-build")

        safe_console_write(f"[server:{inst.name}] starting: {' '.join(server_cmd)}\n")
        inst.server_proc = subprocess.Popen(
            server_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            bufsize=1,
        )
        threading.Thread(
            target=pump_output,
            args=(inst.server_proc, f"[server:{inst.name}] ", None, None, inst.server_keep),
            daemon=True,
        ).start()

    # Wait local health
    for inst in instances:
        if not wait_http_ok(inst.local_base + "/health", timeout_sec=25, proxy=""):
            safe_console_write(f"[FATAL] local server not ready: {inst.name}\n")
            sys.exit(3)

    # Start cloudflared for each
    for inst in instances:
        cf_cmd = [args.cloudflared, "tunnel", "--protocol", args.protocol, "--url", inst.local_base]
        safe_console_write(f"[cf:{inst.name}] starting: {' '.join(cf_cmd)}\n")
        inst.cf_proc = subprocess.Popen(
            cf_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=cf_env,
            bufsize=1,
        )
        threading.Thread(
            target=pump_output,
            args=(inst.cf_proc, f"[cf:{inst.name}] ", inst.url_q, inst.err_q, inst.cf_keep),
            daemon=True,
        ).start()

    # Collect public URLs (no retry)
    deadline = time.time() + 60
    pending = set(instances)
    while pending and time.time() < deadline:
        done = []
        for inst in list(pending):
            if inst.cf_proc and inst.cf_proc.poll() is not None:
                done.append(inst)
                continue
            try:
                _fail = inst.err_q.get_nowait()
                done.append(inst)
                continue
            except Empty:
                pass
            try:
                url = inst.url_q.get_nowait()
                inst.public_url = url
                done.append(inst)
            except Empty:
                pass
        for inst in done:
            pending.discard(inst)
        time.sleep(0.05)

    # Optional public check (warn/strict/off)
    for inst in instances:
        if not inst.public_url:
            safe_console_write(f"[WARN] no public url for {inst.name}\n")
            continue
        if args.public_check == "off":
            continue
        ok = wait_http_ok(inst.public_url + "/health", timeout_sec=float(args.wait_public_seconds), proxy=(args.proxy or ""))
        if not ok:
            safe_console_write(f"[WARN] public /health not OK within {args.wait_public_seconds}s: {inst.name}\n")
            if args.public_check == "strict":
                safe_console_write("[FATAL] strict mode: exiting\n")
                sys.exit(6)

    # Print summary (copy-paste friendly)
    safe_console_write("\n============================================================\n")
    safe_console_write("[READY] Multi-repo links (copy to your LLM)\n\n")
    safe_console_write("Repos:\n")
    for inst in instances:
        if not inst.public_url:
            safe_console_write(f"- {inst.name}: (no public url)\n")
            continue
        safe_console_write(f"- {inst.name}\n")
        safe_console_write(f"  - Index: {inst.public_url}/all\n")
        safe_console_write(f"  - Tree:  {inst.public_url}/tree\n")
        safe_console_write(f"  - File:  {inst.public_url}/file?path=README.md\n")
    safe_console_write("============================================================\n\n")

    safe_console_write("Stop: press Ctrl+C in this terminal.\n")

    try:
        while True:
            time.sleep(1)
            for inst in instances:
                if inst.server_proc and inst.server_proc.poll() is not None:
                    safe_console_write(f"[FATAL] server exited: {inst.name}\n")
                    raise KeyboardInterrupt
                if inst.cf_proc and inst.cf_proc.poll() is not None:
                    safe_console_write(f"[FATAL] cloudflared exited: {inst.name}\n")
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    finally:
        for inst in instances:
            try:
                if inst.cf_proc:
                    inst.cf_proc.terminate()
            except Exception:
                pass
            try:
                if inst.server_proc:
                    inst.server_proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    main()