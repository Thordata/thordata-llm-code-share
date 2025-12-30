#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path
from queue import Queue, Empty
import threading

TRY_URL_RE = re.compile(r"(https://[a-z0-9-]+\.trycloudflare\.com)", re.I)


def http_get(url: str, timeout: float = 5.0, proxy: str = "") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "thordata-llm-code-share/1.0"})

    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        # 关键：禁用系统代理（避免 Clash/系统代理影响健康检查）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def wait_http_ok(url: str, timeout_sec: float = 20.0, interval: float = 0.25, proxy: str = "") -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        try:
            _ = http_get(url, timeout=3.0, proxy=proxy)
            return True
        except Exception:
            time.sleep(interval)
    return False


def port_is_free(bind: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.bind((bind, port))
            return True
        except OSError:
            return False


def pick_free_port(bind: str, start_port: int, max_tries: int = 80) -> int:
    p = start_port
    for _ in range(max_tries):
        if port_is_free(bind, p):
            return p
        p += 1
    raise RuntimeError(f"no free port found starting at {start_port}")


def format_prompt_full(public_url: str, bundle_count: int | None) -> tuple[str, str]:
    index = f"{public_url}/all"
    tree = f"{public_url}/tree"
    parts = []
    if bundle_count and bundle_count > 0:
        parts = [f"{public_url}/all?part={i}" for i in range(1, bundle_count + 1)]

    # English
    en = []
    en.append("You are given a code repository snapshot exposed via a read-only text server.")
    en.append("Please read the repository and then answer my questions.")
    en.append("")
    en.append("Rules:")
    en.append("1) Start by reading the index URL /all. It lists how many parts exist.")
    en.append("2) Then fetch parts in order from part=1..N (or until you have enough context).")
    en.append("3) When you cite code, mention the file path shown in the bundle (e.g. FILE: src/...).")
    en.append("")
    en.append("URLs:")
    en.append(f"- Index: {index}")
    en.append(f"- Tree:  {tree}")
    if parts:
        en.append("- Parts:")
        for u in parts:
            en.append(f"  - {u}")
    else:
        en.append("- Parts: (open the index to see the list)")

    # Chinese
    zh = []
    zh.append("下面是一个只读的“代码文本服务”，里面包含仓库的快照。请先通读再回答我的问题。")
    zh.append("")
    zh.append("阅读规则：")
    zh.append("1)先读索引 /all,它会告诉你一共有几片 part。")
    zh.append("2)再按顺序读取 part=1..N(或读到足够为止)。")
    zh.append("3)引用代码时请带上文件路径(bundle 里有 FILE: ...）。")
    zh.append("")
    zh.append("链接：")
    zh.append(f"- 索引: {index}")
    zh.append(f"- 结构: {tree}")
    if parts:
        zh.append("- 分片：")
        for u in parts:
            zh.append(f"  - {u}")
    else:
        zh.append("- 分片：请打开索引查看")

    return "\n".join(en), "\n".join(zh)


def format_prompt_precise(public_url: str) -> tuple[str, str]:
    tree = f"{public_url}/tree"

    # English
    en = []
    en.append("You are given a code repository snapshot exposed via a read-only text server.")
    en.append("Please answer my questions by reading only the necessary files.")
    en.append("")
    en.append("Rules:")
    en.append("1) Start with /tree to see all files.")
    en.append("2) Then fetch specific files via /file?path=... as needed.")
    en.append("3) If you need broad context, you may additionally use /all and /all?part=N.")
    en.append("4) When you cite code, mention the file path shown in the response (FILE: ...).")
    en.append("")
    en.append("URLs:")
    en.append(f"- Tree: {tree}")
    en.append(f"- File: {public_url}/file?path=relative/path/to/file.py")

    # Chinese
    zh = []
    zh.append("下面是一个只读的“代码文本服务”。请尽量只读取必要文件，再回答我的问题。")
    zh.append("")
    zh.append("阅读规则：")
    zh.append("1)先读 /tree 获取文件清单。")
    zh.append("2)再用 /file?path=... 按需读取具体文件内容。")
    zh.append("3)如果需要更广的上下文，再补充读取 /all 和 /all?part=N。")
    zh.append("4)引用代码时请带上文件路径（响应中有 FILE: ...）。")
    zh.append("")
    zh.append("链接：")
    zh.append(f"- 文件树: {tree}")
    zh.append(f"- 读文件: {public_url}/file?path=relative/path/to/file.py")

    return "\n".join(en), "\n".join(zh)


def pump_process_output(
    *,
    proc: subprocess.Popen,
    name: str,
    url_queue: Queue | None = None,
    keep_last: deque | None = None,
) -> None:
    """
    Continuously read proc.stdout to avoid blocking child process.
    Optionally parse trycloudflare URL and push to url_queue.
    """
    try:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            sys.stdout.write(line)
            sys.stdout.flush()

            if keep_last is not None:
                keep_last.append(line.rstrip("\n"))

            if url_queue is not None:
                m = TRY_URL_RE.search(line)
                if m:
                    url_queue.put(m.group(1))
    except Exception as e:
        sys.stdout.write(f"[WARN] output pump for {name} stopped: {e}\n")
        sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="repo root path, e.g. /d/Thordata_Work/thordata-python-sdk")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--auto-port", action="store_true", help="if port is busy, auto pick next free port")
    ap.add_argument("--chunk-bytes", type=int, default=900000, help="recommended: 600000~1200000")
    ap.add_argument("--max-single-file-bytes", type=int, default=3000000)
    ap.add_argument("--cache-dirname", default=".llm_cache")

    ap.add_argument("--cloudflared", default="cloudflared", help="path or command name of cloudflared")
    ap.add_argument("--protocol", default="http2", choices=["http2", "quic"])
    ap.add_argument("--proxy", default="", help="optional http proxy for cloudflared, e.g. http://127.0.0.1:7897")

    ap.add_argument("--no-warmup", action="store_true", help="skip warmup build (not recommended)")
    ap.add_argument("--auto-build", action="store_true", help="server auto build cache on first /all if missing")
    ap.add_argument("--server-script", default=None, help="path to llm_server.py (default: same dir as this script)")

    ap.add_argument("--open", action="store_true", help="open public /all in your default browser")
    ap.add_argument("--wait-public-seconds", type=float, default=60.0, help="wait for public /health to become OK")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"[FATAL] --root not found: {root}")
        sys.exit(2)

    this_dir = Path(__file__).resolve().parent
    server_script = Path(args.server_script).resolve() if args.server_script else (this_dir / "llm_server.py")
    if not server_script.exists():
        print(f"[FATAL] llm_server.py not found: {server_script}")
        sys.exit(2)

    port = args.port
    if not port_is_free(args.bind, port):
        if args.auto_port:
            port = pick_free_port(args.bind, port)
            print(f"[WARN] port {args.port} busy, auto picked free port: {port}")
        else:
            print(f"[FATAL] port {port} is busy. Use --auto-port or choose another --port")
            sys.exit(2)

    local_base = f"http://{args.bind}:{port}"

    print("============================================================")
    print("thordata-llm-code-share: Quick Tunnel Launcher (stable)")
    print("ROOT :", root)
    print("LOCAL :", local_base)
    print("")
    print("Recommended tuning:")
    print("  --chunk-bytes:")
    print("      600000  (more stable, more parts)")
    print("      900000  (default, balanced)")
    print("     1200000  (fewer parts, may timeout for some LLM fetchers)")
    print(f"  current: chunk-bytes={args.chunk_bytes}, max-single-file-bytes={args.max_single_file_bytes}")
    print("============================================================")
    print("")

    # Force UTF-8 for child processes (Windows console often GBK)
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"

    # 1) start server
    server_cmd = [
        sys.executable, str(server_script),
        "--root", root,
        "--bind", args.bind,
        "--port", str(port),
        "--cache-dirname", args.cache_dirname,
        "--chunk-bytes", str(args.chunk_bytes),
        "--max-single-file-bytes", str(args.max_single_file_bytes),
    ]
    if not args.no_warmup:
        server_cmd.append("--warmup")
    if args.auto_build:
        server_cmd.append("--auto-build")

    print("[1/4] Starting server:")
    print(" ", " ".join(server_cmd))

    server_keep_last = deque(maxlen=200)
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

    server_pump_t = threading.Thread(
        target=pump_process_output,
        kwargs={"proc": server_proc, "name": "server", "keep_last": server_keep_last},
        daemon=True,
    )
    server_pump_t.start()

    if not wait_http_ok(local_base + "/health", timeout_sec=25, proxy=""):
        print("[FATAL] Server did not become ready at /health within timeout.")
        print("---- server output (last lines) ----")
        for line in list(server_keep_last)[-120:]:
            print(line)
        try:
            server_proc.terminate()
        except Exception:
            pass
        sys.exit(3)

    # If no warmup, build now (heavy)
    if args.no_warmup:
        print("[2/4] Building cache via /build ...")
        try:
            meta_txt = http_get(local_base + "/build", timeout=120.0)
            _ = json.loads(meta_txt)
        except Exception as e:
            print("[WARN] /build failed:", e)
            print("You can still continue, but /all may say 'No cache yet'.")

    # 2) start cloudflared and parse public url
    print("[3/4] Starting cloudflared quick tunnel:")
    cf_cmd = [
        args.cloudflared,
        "tunnel",
        "--protocol", args.protocol,
        "--url", f"http://{args.bind}:{port}",
    ]
    print(" ", " ".join(cf_cmd))

    cf_env = os.environ.copy()
    if args.proxy:
        # Set both lower/upper for compatibility
        cf_env["http_proxy"] = args.proxy
        cf_env["https_proxy"] = args.proxy
        cf_env["HTTP_PROXY"] = args.proxy
        cf_env["HTTPS_PROXY"] = args.proxy
        cf_env["NO_PROXY"] = "127.0.0.1,localhost"

    try:
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
    except FileNotFoundError:
        print("[FATAL] cloudflared not found. Check PATH or pass --cloudflared /path/to/cloudflared")
        try:
            server_proc.terminate()
        except Exception:
            pass
        sys.exit(4)

    url_queue: Queue[str] = Queue()
    cf_keep_last = deque(maxlen=400)

    cf_pump_t = threading.Thread(
        target=pump_process_output,
        kwargs={"proc": cf_proc, "name": "cloudflared", "url_queue": url_queue, "keep_last": cf_keep_last},
        daemon=True,
    )
    cf_pump_t.start()

    public_url = None
    try:
        public_url = url_queue.get(timeout=40)
    except Empty:
        public_url = None

    if not public_url:
        print("[FATAL] Could not parse trycloudflare URL from cloudflared output within timeout.")
        print("---- cloudflared output (last lines) ----")
        for line in list(cf_keep_last)[-120:]:
            print(line)
        try:
            cf_proc.terminate()
        except Exception:
            pass
        try:
            server_proc.terminate()
        except Exception:
            pass
        sys.exit(5)

    # 2.5) Wait until public URL really works
    pub_health = public_url + "/health"
    print(f"[3.5/4] Waiting for public health OK: {pub_health}")
    if not wait_http_ok(pub_health, timeout_sec=float(args.wait_public_seconds), interval=0.5, proxy=(args.proxy or "")):
        print("[FATAL] Public /health did not become reachable within timeout.")
        print("This usually means the tunnel is not healthy (may lead to Cloudflare 1033).")
        print("---- cloudflared output (last lines) ----")
        for line in list(cf_keep_last)[-160:]:
            print(line)
        try:
            cf_proc.terminate()
        except Exception:
            pass
        try:
            server_proc.terminate()
        except Exception:
            pass
        sys.exit(6)

    # 3) read meta to know bundle count
    bundle_count = None
    try:
        meta_txt = http_get(local_base + "/meta", timeout=5.0)
        meta = json.loads(meta_txt)
        bundle_count = int(meta.get("bundle_count", 0))
    except Exception:
        pass

    # Print shareable URLs
    print("\n============================================================")
    print("[READY] Share these URLs with your LLM:")
    print("")
    print("Health:")
    print(f"  {public_url}/health")
    print("")
    print("Index (tells how many parts):")
    print(f"  {public_url}/all")
    print("")
    print("Optional (structure):")
    print(f"  {public_url}/tree")
    print("")
    print("Example file fetch:")
    print(f"  {public_url}/file?path=README.md")
    print("============================================================")

    # Prompt templates (Full + Precise)
    en_full, zh_full = format_prompt_full(public_url, bundle_count)
    en_precise, zh_precise = format_prompt_precise(public_url)

    print("\n[Copy-paste prompt template - Full snapshot / English]")
    print("------------------------------------------------------------")
    print(en_full)
    print("------------------------------------------------------------")

    print("\n[复制粘贴给大模型的提示词模板 - 全量通读 / 中文]")
    print("------------------------------------------------------------")
    print(zh_full)
    print("------------------------------------------------------------")

    print("\n[Copy-paste prompt template - Precise files / English]")
    print("------------------------------------------------------------")
    print(en_precise)
    print("------------------------------------------------------------")

    print("\n[复制粘贴给大模型的提示词模板 - 精准读文件 / 中文]")
    print("------------------------------------------------------------")
    print(zh_precise)
    print("------------------------------------------------------------\n")

    # Auto open browser
    if args.open:
        target = f"{public_url}/all"
        print(f"[OPEN] opening browser: {target}")
        try:
            webbrowser.open(target, new=2)
        except Exception as e:
            print("[WARN] failed to open browser:", e)

    print("Stop: press Ctrl+C in this terminal.\n")

    # keep running
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