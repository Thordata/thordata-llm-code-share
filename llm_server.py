#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
thordata-llm-code-share: local repo -> LLM-friendly text endpoints
Features:
  - /build: build chunked bundles into cache dir
  - /all: FAST index (seconds to return)
  - /all?part=N: FAST chunk fetch
  - /tree: file list (filtered)
  - /file?path=...: fetch single file (filtered)
Security:
  - blocks common secrets: .env/.pem/.key/etc
  - ignores dependencies/build outputs: node_modules/target/vendor/dist/...
Notes:
  - bind defaults to 127.0.0.1 for safety; expose via cloudflared if needed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable, Optional, Tuple


# -------------------------
# Defaults (safe-by-default)
# -------------------------

DEFAULT_PORT = 8080
DEFAULT_BIND = "127.0.0.1"
DEFAULT_CACHE_DIRNAME = ".llm_cache"

# 建议 chunk 0.6MB~1.2MB：太大容易被抓取器/链路超时；太小 part 太多
DEFAULT_CHUNK_BYTES = 600_000

# 单文件太大（比如巨型 JSON/YAML/spec），直接截断，避免撑爆 chunk/耗时
DEFAULT_MAX_SINGLE_FILE_BYTES = 3_000_000

# 只要你不是必须把 lock 文件喂给模型，建议忽略：它们很吵且体积可能大
IGNORE_LOCK_FILES_BY_DEFAULT = True


# -------------------------
# Ignore rules (跨语言通用)
# -------------------------

IGNORE_DIRS_EXACT = {
    # VCS
    ".git", ".svn", ".hg",

    # our cache (avoid recursion)
    DEFAULT_CACHE_DIRNAME,

    # IDE / Editor
    ".idea", ".vscode", ".vs", ".fleet", ".settings",

    # Python
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "venv", ".venv", "env", ".envdir",
    "build", "dist", ".eggs",

    # Node / frontend
    "node_modules", ".npm", ".yarn", ".pnpm-store",
    ".next", ".nuxt", ".svelte-kit", ".astro", ".cache",
    "coverage", ".nyc_output",

    # Go
    "vendor", "bin", "out",

    # Java / JVM
    "target", ".gradle", "gradle", "out",

    # Logs / temp
    "logs", "log", "tmp", "temp",
}

IGNORE_DIRS_REGEX = [
    re.compile(r".*\.egg-info$", re.IGNORECASE),
]

# 绝对要挡的敏感文件名
IGNORE_FILES_EXACT = {
    # secrets / env
    ".env", ".env.local", ".env.dev", ".env.prod", ".env.test",
    ".env.staging", ".env.production",
    ".npmrc", ".pypirc",

    # common private key names
    "id_rsa", "id_ed25519",

    # coverage artifacts
    ".coverage", "coverage.xml", "lcov.info",

    # OS junk
    "Thumbs.db", "Desktop.ini", ".DS_Store",

    # metadata
    "SOURCES.txt", "PKG-INFO",

    ".git",          # submodule often has .git as a FILE
    ".gitmodules",   # optional: reduce noise
}

# 常见敏感/二进制后缀：直接不让读
IGNORE_EXTS = {
    # binaries / libs
    ".exe", ".dll", ".so", ".dylib",

    # archives
    ".zip", ".7z", ".rar", ".tar", ".gz",

    # images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",

    # media
    ".mp4", ".mp3", ".wav",

    # secrets / certs
    ".pem", ".key", ".p12", ".pfx",

    # misc binaries
    ".class", ".jar",
}

IGNORE_FILE_REGEX = [
    re.compile(r".*\.log$", re.IGNORECASE),
]


def is_ignored_dir(dirname: str) -> bool:
    if dirname in IGNORE_DIRS_EXACT:
        return True
    for rg in IGNORE_DIRS_REGEX:
        if rg.match(dirname):
            return True
    return False


def is_ignored_file(filename: str, ignore_lock_files: bool) -> bool:
    if filename in IGNORE_FILES_EXACT:
        return True
    if ignore_lock_files and filename.lower().endswith(".lock"):
        return True
    for rg in IGNORE_FILE_REGEX:
        if rg.match(filename):
            return True
    return False


def is_ignored_ext(ext: str) -> bool:
    return ext.lower() in IGNORE_EXTS


def looks_binary(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
        return b"\x00" in chunk
    except Exception:
        # 读不了就当作不可读/二进制处理
        return True


def safe_read_text(path: str, max_bytes: Optional[int] = None) -> str:
    with open(path, "rb") as f:
        data = f.read() if max_bytes is None else f.read(max_bytes)

    # 尽量保证永不抛异常
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return data.decode("latin-1", errors="replace")


def iter_repo_files(root_dir: str, ignore_lock_files: bool) -> Iterable[Tuple[str, str]]:
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not is_ignored_dir(d)]
        for name in files:
            if is_ignored_file(name, ignore_lock_files=ignore_lock_files):
                continue

            ext = os.path.splitext(name)[1].lower()
            if is_ignored_ext(ext):
                continue

            full = os.path.join(root, name)

            try:
                if os.path.islink(full):
                    continue
            except Exception:
                pass

            rel = os.path.relpath(full, root_dir)
            yield rel, full


def fingerprint_file_list(rel_paths: list[str]) -> str:
    h = hashlib.sha1()
    for p in rel_paths:
        h.update(p.encode("utf-8", "ignore"))
        h.update(b"\n")
    return h.hexdigest()


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def build_bundles(
    *,
    root_dir: str,
    cache_dir: str,
    chunk_bytes: int,
    max_single_file_bytes: int,
    ignore_lock_files: bool,
) -> dict:
    """
    Build chunked bundles:
      cache_dir/
        meta.json
        index.txt
        bundle_0001.txt
        bundle_0002.txt
        ...
    """
    ensure_dir(cache_dir)

    # 构建文件列表与 fingerprint
    rel_paths: list[str] = []
    for rel, _full in iter_repo_files(root_dir, ignore_lock_files=ignore_lock_files):
        rel_paths.append(rel)
    rel_paths.sort()

    fp = fingerprint_file_list(rel_paths)
    started = time.time()

    parts: list[str] = []
    current = io.StringIO()
    current_size = 0
    files_included = 0

    header = (
        f"# LLM BUNDLE (chunked)\n"
        f"# ROOT: {root_dir}\n"
        f"# GENERATED_AT: {datetime.now().isoformat(timespec='seconds')}\n"
        f"# FINGERPRINT: {fp}\n"
        f"# NOTE: Fetch /all for index, then /all?part=N for chunks.\n\n"
    )
    current.write(header)
    current_size += len(header.encode("utf-8", "replace"))

    def flush_part():
        nonlocal current, current_size
        if current_size <= 0:
            return
        parts.append(current.getvalue())
        current = io.StringIO()
        current_size = 0

    # 逐文件写入分片
    for rel, full in iter_repo_files(root_dir, ignore_lock_files=ignore_lock_files):
        if looks_binary(full):
            continue

        try:
            size = os.path.getsize(full)
        except Exception:
            size = -1

        truncated = False
        read_limit = None
        if size >= 0 and size > max_single_file_bytes:
            truncated = True
            read_limit = max_single_file_bytes

        try:
            content = safe_read_text(full, max_bytes=read_limit)
        except Exception:
            continue

        block_header = (
            f"\n\n{'='*72}\n"
            f"FILE: {rel}\n"
            f"SIZE: {size}\n"
            f"{'TRUNCATED: yes' if truncated else 'TRUNCATED: no'}\n"
            f"{'='*72}\n"
        )
        block = block_header + content
        block_bytes = block.encode("utf-8", "replace")

        # 不够放：先 flush
        if current_size + len(block_bytes) > chunk_bytes and current_size > 0:
            flush_part()

        current.write(block)
        current_size += len(block_bytes)
        files_included += 1

    flush_part()

    # 写 bundle 文件
    bundle_files: list[str] = []
    for i, txt in enumerate(parts, start=1):
        name = f"bundle_{i:04d}.txt"
        path = os.path.join(cache_dir, name)
        with open(path, "wb") as f:
            f.write(txt.encode("utf-8", "replace"))
        bundle_files.append(name)

    meta = {
        "root": root_dir,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "fingerprint": fp,
        "chunk_bytes": chunk_bytes,
        "max_single_file_bytes": max_single_file_bytes,
        "ignore_lock_files": ignore_lock_files,
        "bundle_count": len(bundle_files),
        "bundle_files": bundle_files,
        "files_included": files_included,
        "build_seconds": round(time.time() - started, 3),
    }

    with open(os.path.join(cache_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # index.txt: /all 秒回就靠它
    idx = io.StringIO()
    idx.write("# /all INDEX (FAST)\n")
    idx.write(f"# generated_at: {meta['generated_at']}\n")
    idx.write(f"# fingerprint: {meta['fingerprint']}\n")
    idx.write(f"# bundles: {meta['bundle_count']}\n")
    idx.write(f"# build_seconds: {meta['build_seconds']}\n\n")
    idx.write("How to read:\n")
    idx.write("  1) GET /all  -> this index\n")
    idx.write("  2) GET /all?part=N  (N starts at 1)\n")
    idx.write("  3) Or use /tree + /file?path=... for precise reading\n\n")
    idx.write("Chunks:\n")
    for n, name in enumerate(bundle_files, start=1):
        idx.write(f"  - part={n}\tGET /all?part={n}\t({name})\n")

    with open(os.path.join(cache_dir, "index.txt"), "wb") as f:
        f.write(idx.getvalue().encode("utf-8", "replace"))

    return meta


# -------------------------
# HTTP server
# -------------------------

build_lock = threading.Lock()


class RepoServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address,
        handler_cls,
        *,
        root_dir: str,
        cache_dir: str,
        chunk_bytes: int,
        max_single_file_bytes: int,
        ignore_lock_files: bool,
        auto_build: bool,
    ):
        super().__init__(server_address, handler_cls)
        self.root_dir = root_dir
        self.cache_dir = cache_dir
        self.chunk_bytes = chunk_bytes
        self.max_single_file_bytes = max_single_file_bytes
        self.ignore_lock_files = ignore_lock_files
        self.auto_build = auto_build


class Handler(SimpleHTTPRequestHandler):
    server_version = "ThordataLLMCodeShare/1.0"

    def _send_text_headers(self, code=200, extra_headers: Optional[dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()

    def _safe_write(self, b: bytes) -> bool:
        try:
            self.wfile.write(b)
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端中断（Windows 常见 10053）：安静结束
            return False

    def _send_file_fast(self, path: str):
        try:
            st = os.stat(path)
            self._send_text_headers(200, extra_headers={"Content-Length": str(st.st_size)})
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 256)
                    if not chunk:
                        break
                    if not self._safe_write(chunk):
                        return
        except FileNotFoundError:
            self._send_text_headers(404)
            self._safe_write(b"not found\n")
        except Exception as e:
            self._send_text_headers(500)
            self._safe_write(f"error: {e}\n".encode("utf-8", "replace"))

    def do_GET(self):
        root_dir = self.server.root_dir
        cache_dir = self.server.cache_dir

        url = urllib.parse.urlparse(self.path)
        path = url.path
        qs = urllib.parse.parse_qs(url.query)

        if path == "/health":
            self._send_text_headers(200)
            self._safe_write(b"ok\n")
            return

        if path == "/robots.txt":
            # 避免一些探测器反复访问导致噪音
            self._send_text_headers(200)
            self._safe_write(b"User-agent: *\nDisallow: /\n")
            return

        if path == "/":
            self._send_text_headers(200)
            msg = (
                "thordata-llm-code-share running.\n\n"
                "Endpoints:\n"
                "  /tree\n"
                "  /file?path=relative/path/to/file\n"
                "  /build (heavy)\n"
                "  /meta\n"
                "  /all (FAST index)\n"
                "  /all?part=N\n"
                "  /health\n"
            )
            self._safe_write(msg.encode("utf-8"))
            return

        if path == "/tree":
            self._send_text_headers(200)
            out = io.StringIO()
            out.write(f"# TREE: {root_dir}\n")
            out.write("# rel_path\tsize_bytes\n")
            for rel, full in iter_repo_files(root_dir, ignore_lock_files=self.server.ignore_lock_files):
                try:
                    size = os.path.getsize(full)
                except Exception:
                    size = -1
                out.write(f"{rel}\t{size}\n")
            self._safe_write(out.getvalue().encode("utf-8", "replace"))
            return

        if path == "/file":
            rel = (qs.get("path", [""])[0] or "").strip().lstrip("/\\")
            if not rel:
                self._send_text_headers(400)
                self._safe_write(b"missing query param: ?path=\n")
                return

            full = os.path.abspath(os.path.join(root_dir, rel))
            if os.path.commonpath([os.path.abspath(root_dir), full]) != os.path.abspath(root_dir):
                self._send_text_headers(403)
                self._safe_write(b"path escapes root\n")
                return

            if not os.path.isfile(full):
                self._send_text_headers(404)
                self._safe_write(b"file not found\n")
                return

            name = os.path.basename(full)
            ext = os.path.splitext(name)[1].lower()
            if is_ignored_file(name, ignore_lock_files=self.server.ignore_lock_files) or is_ignored_ext(ext) or looks_binary(full):
                self._send_text_headers(403)
                self._safe_write(b"file blocked by ignore/binary rules\n")
                return

            self._send_text_headers(200)
            header = f"{'='*72}\nFILE: {rel}\n{'='*72}\n"
            if not self._safe_write(header.encode("utf-8", "replace")):
                return
            try:
                content = safe_read_text(full)
            except Exception as e:
                self._safe_write(f"(read error) {e}\n".encode("utf-8", "replace"))
                return
            self._safe_write(content.encode("utf-8", "replace"))
            return

        if path == "/build":
            refresh = (qs.get("refresh", ["0"])[0] == "1")
            meta_path = os.path.join(cache_dir, "meta.json")
            with build_lock:
                if refresh or (not os.path.exists(meta_path)):
                    meta = build_bundles(
                        root_dir=root_dir,
                        cache_dir=cache_dir,
                        chunk_bytes=self.server.chunk_bytes,
                        max_single_file_bytes=self.server.max_single_file_bytes,
                        ignore_lock_files=self.server.ignore_lock_files,
                    )
                else:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
            self._send_text_headers(200)
            self._safe_write(json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
            return

        if path == "/meta":
            meta_path = os.path.join(cache_dir, "meta.json")
            if not os.path.exists(meta_path):
                self._send_text_headers(404)
                self._safe_write(b"no meta.json; run /build first\n")
                return
            self._send_file_fast(meta_path)
            return

        if path == "/all":
            meta_path = os.path.join(cache_dir, "meta.json")
            index_path = os.path.join(cache_dir, "index.txt")

            # 自动 build（可选）：避免你忘了 warmup/build
            if self.server.auto_build and (not os.path.exists(meta_path) or not os.path.exists(index_path)):
                with build_lock:
                    if not os.path.exists(meta_path) or not os.path.exists(index_path):
                        build_bundles(
                            root_dir=root_dir,
                            cache_dir=cache_dir,
                            chunk_bytes=self.server.chunk_bytes,
                            max_single_file_bytes=self.server.max_single_file_bytes,
                            ignore_lock_files=self.server.ignore_lock_files,
                        )

            if not os.path.exists(meta_path) or not os.path.exists(index_path):
                self._send_text_headers(200)
                self._safe_write(b"# No cache yet. Run: GET /build\n")
                return

            part = qs.get("part", [None])[0]
            if part is None:
                # index (FAST)
                self._send_file_fast(index_path)
                return

            # part (FAST)
            try:
                part_num = int(part)
            except ValueError:
                self._send_text_headers(400)
                self._safe_write(b"bad part number\n")
                return

            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            files = meta.get("bundle_files", [])
            if part_num < 1 or part_num > len(files):
                self._send_text_headers(404)
                self._safe_write(b"part out of range\n")
                return

            bundle_path = os.path.join(cache_dir, files[part_num - 1])
            self._send_file_fast(bundle_path)
            return

        # 默认不开放目录浏览（更安全）。如果你想开放，可以删掉下面 3 行并调用 super().do_GET()
        self._send_text_headers(404)
        self._safe_write(b"not found\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.getcwd(), help="repo root (default: cwd)")
    ap.add_argument("--bind", default=DEFAULT_BIND, help="bind address (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--cache-dirname", default=DEFAULT_CACHE_DIRNAME, help="cache dir under root")
    ap.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    ap.add_argument("--max-single-file-bytes", type=int, default=DEFAULT_MAX_SINGLE_FILE_BYTES)
    ap.add_argument("--no-lock-ignore", action="store_true", help="do not ignore *.lock files")
    ap.add_argument("--warmup", action="store_true", help="build cache at startup (recommended)")
    ap.add_argument("--auto-build", action="store_true", help="auto build on first /all if missing")
    ap.add_argument("--exclude-github", action="store_true", help="exclude .github directory")
    args = ap.parse_args()

    if args.exclude_github:
        IGNORE_DIRS_EXACT.add(".github")

    IGNORE_DIRS_EXACT.add(args.cache_dirname)

    root_dir = os.path.abspath(args.root)
    cache_dir = os.path.join(root_dir, args.cache_dirname)
    ignore_lock_files = (not args.no_lock_ignore) and IGNORE_LOCK_FILES_BY_DEFAULT

    # warmup build
    if args.warmup:
        with build_lock:
            build_bundles(
                root_dir=root_dir,
                cache_dir=cache_dir,
                chunk_bytes=args.chunk_bytes,
                max_single_file_bytes=args.max_single_file_bytes,
                ignore_lock_files=ignore_lock_files,
            )

    httpd = RepoServer(
        (args.bind, args.port),
        Handler,
        root_dir=root_dir,
        cache_dir=cache_dir,
        chunk_bytes=args.chunk_bytes,
        max_single_file_bytes=args.max_single_file_bytes,
        ignore_lock_files=ignore_lock_files,
        auto_build=args.auto_build,
    )

    print(f"[OK] ROOT: {root_dir}")
    print(f"[OK] LOCAL: http://{args.bind}:{args.port}")
    print("Endpoints: /build /all /all?part=N /tree /file?path=... /meta /health")
    print("Safety: blocks .env/.pem/.key + ignores node_modules/target/vendor/dist/... by default.")
    httpd.serve_forever()


if __name__ == "__main__":
    main()