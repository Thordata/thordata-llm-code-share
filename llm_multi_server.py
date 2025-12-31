#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import io
import json
import os
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import llm_server as core  # reuse your existing logic


@dataclass
class RepoSpec:
    name: str
    root_dir: str
    cache_dir: str
    chunk_bytes: int
    max_single_file_bytes: int
    ignore_lock_files: bool
    auto_build: bool
    lock: threading.Lock


class MultiRepoServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_cls, *, repos: dict[str, RepoSpec]):
        super().__init__(server_address, handler_cls)
        self.repos = repos


class Handler(SimpleHTTPRequestHandler):
    server_version = "ThordataLLMCodeShareMulti/1.0"

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

    def _list_repos_text(self) -> str:
        out = io.StringIO()
        out.write("thordata-llm-code-share (multi-repo) running.\n\n")
        out.write("Endpoints:\n")
        out.write("  /health\n")
        out.write("  /repos\n")
        out.write("  /r/<repo>/tree\n")
        out.write("  /r/<repo>/file?path=...\n")
        out.write("  /r/<repo>/build\n")
        out.write("  /r/<repo>/meta\n")
        out.write("  /r/<repo>/all\n")
        out.write("  /r/<repo>/all?part=N\n\n")
        out.write("Repos:\n")
        for name, repo in self.server.repos.items():
            out.write(f"  - {name}\t{repo.root_dir}\n")
        out.write("\n")
        out.write("Tip:\n")
        out.write("  Start with /repos, then choose /r/<repo>/all (full) or /r/<repo>/tree + /file (precise).\n")
        return out.getvalue()

    def _get_repo_and_subpath(self, path: str):
        # expected: /r/<name>/...
        parts = path.split("/")
        # ['', 'r', '<name>', ...]
        if len(parts) < 3 or parts[1] != "r":
            return None, None
        name = parts[2]
        repo = self.server.repos.get(name)
        if not repo:
            return None, None
        sub = "/" + "/".join(parts[3:])  # might be "/" if empty
        if sub == "/":
            sub = "/"
        return repo, sub

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        qs = urllib.parse.parse_qs(url.query)

        if path == "/health":
            self._send_text_headers(200)
            self._safe_write(b"ok\n")
            return

        if path == "/robots.txt":
            self._send_text_headers(200)
            self._safe_write(b"User-agent: *\nDisallow: /\n")
            return

        if path == "/" or path == "/repos":
            self._send_text_headers(200)
            self._safe_write(self._list_repos_text().encode("utf-8", "replace"))
            return

        repo, subpath = self._get_repo_and_subpath(path)
        if repo is None:
            self._send_text_headers(404)
            self._safe_write(b"not found (repo missing or bad path)\n")
            return

        root_dir = repo.root_dir
        cache_dir = repo.cache_dir

        # Repo root page
        if subpath == "/" or subpath == "":
            self._send_text_headers(200)
            msg = (
                f"Repo: {repo.name}\n"
                f"Root: {root_dir}\n\n"
                f"Endpoints:\n"
                f"  /r/{repo.name}/tree\n"
                f"  /r/{repo.name}/file?path=relative/path\n"
                f"  /r/{repo.name}/build\n"
                f"  /r/{repo.name}/meta\n"
                f"  /r/{repo.name}/all\n"
                f"  /r/{repo.name}/all?part=N\n"
            )
            self._safe_write(msg.encode("utf-8", "replace"))
            return

        if subpath == "/tree":
            self._send_text_headers(200)
            out = io.StringIO()
            out.write(f"# REPO: {repo.name}\n")
            out.write(f"# TREE: {root_dir}\n")
            out.write("# rel_path\tsize_bytes\n")
            for rel, full in core.iter_repo_files(root_dir, ignore_lock_files=repo.ignore_lock_files):
                try:
                    size = os.path.getsize(full)
                except Exception:
                    size = -1
                out.write(f"{rel}\t{size}\n")
            self._safe_write(out.getvalue().encode("utf-8", "replace"))
            return

        if subpath == "/file":
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
            if core.is_ignored_file(name, ignore_lock_files=repo.ignore_lock_files) or core.is_ignored_ext(ext) or core.looks_binary(full):
                self._send_text_headers(403)
                self._safe_write(b"file blocked by ignore/binary rules\n")
                return

            self._send_text_headers(200)
            header = f"{'='*72}\nREPO: {repo.name}\nFILE: {rel}\n{'='*72}\n"
            if not self._safe_write(header.encode("utf-8", "replace")):
                return
            try:
                content = core.safe_read_text(full)
            except Exception as e:
                self._safe_write(f"(read error) {e}\n".encode("utf-8", "replace"))
                return
            self._safe_write(content.encode("utf-8", "replace"))
            return

        if subpath == "/build":
            refresh = (qs.get("refresh", ["0"])[0] == "1")
            meta_path = os.path.join(cache_dir, "meta.json")
            with repo.lock:
                if refresh or (not os.path.exists(meta_path)):
                    meta = core.build_bundles(
                        root_dir=root_dir,
                        cache_dir=cache_dir,
                        chunk_bytes=repo.chunk_bytes,
                        max_single_file_bytes=repo.max_single_file_bytes,
                        ignore_lock_files=repo.ignore_lock_files,
                    )
                else:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
            self._send_text_headers(200)
            self._safe_write(json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8", "replace"))
            return

        if subpath == "/meta":
            meta_path = os.path.join(cache_dir, "meta.json")
            if not os.path.exists(meta_path):
                self._send_text_headers(404)
                self._safe_write(b"no meta.json; run /build first\n")
                return
            self._send_file_fast(meta_path)
            return

        if subpath == "/all":
            meta_path = os.path.join(cache_dir, "meta.json")
            index_path = os.path.join(cache_dir, "index.txt")

            if repo.auto_build and (not os.path.exists(meta_path) or not os.path.exists(index_path)):
                with repo.lock:
                    if not os.path.exists(meta_path) or not os.path.exists(index_path):
                        core.build_bundles(
                            root_dir=root_dir,
                            cache_dir=cache_dir,
                            chunk_bytes=repo.chunk_bytes,
                            max_single_file_bytes=repo.max_single_file_bytes,
                            ignore_lock_files=repo.ignore_lock_files,
                        )

            if not os.path.exists(meta_path) or not os.path.exists(index_path):
                self._send_text_headers(200)
                self._safe_write(b"# No cache yet. Run: GET /r/<repo>/build\n")
                return

            part = qs.get("part", [None])[0]
            if part is None:
                self._send_file_fast(index_path)
                return

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

        self._send_text_headers(404)
        self._safe_write(b"not found\n")


def parse_repo_arg(s: str) -> tuple[str, str]:
    # accept "name=path" or just "path"
    if "=" in s:
        name, path = s.split("=", 1)
        return name.strip(), path.strip()
    p = s.strip()
    name = os.path.basename(p.rstrip("/\\"))
    return name, p


def uniquify_names(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen = {}
    out = []
    for name, path in items:
        base = name
        i = seen.get(base, 0)
        if i == 0:
            seen[base] = 1
            out.append((base, path))
        else:
            seen[base] = i + 1
            out.append((f"{base}-{i+1}", path))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", action="append", required=True, help='repeatable: "name=/path" or "/path"')
    ap.add_argument("--bind", default=core.DEFAULT_BIND)
    ap.add_argument("--port", type=int, default=core.DEFAULT_PORT)
    ap.add_argument("--cache-dirname", default=core.DEFAULT_CACHE_DIRNAME)
    ap.add_argument("--chunk-bytes", type=int, default=core.DEFAULT_CHUNK_BYTES)
    ap.add_argument("--max-single-file-bytes", type=int, default=core.DEFAULT_MAX_SINGLE_FILE_BYTES)
    ap.add_argument("--no-lock-ignore", action="store_true")
    ap.add_argument("--warmup", action="store_true")
    ap.add_argument("--auto-build", action="store_true")
    ap.add_argument("--exclude-github", action="store_true")
    args = ap.parse_args()

    if args.exclude_github:
        core.IGNORE_DIRS_EXACT.add(".github")

    # ensure cache dir ignored
    core.IGNORE_DIRS_EXACT.add(args.cache_dirname)

    ignore_lock_files = (not args.no_lock_ignore) and core.IGNORE_LOCK_FILES_BY_DEFAULT

    pairs = [parse_repo_arg(x) for x in args.repo]
    pairs = uniquify_names(pairs)

    repos: dict[str, RepoSpec] = {}
    for name, path in pairs:
        root = os.path.abspath(path)
        if not os.path.isdir(root):
            print(f"[SKIP] repo not found: {name} -> {root}")
            continue
        cache_dir = os.path.join(root, args.cache_dirname)
        repos[name] = RepoSpec(
            name=name,
            root_dir=root,
            cache_dir=cache_dir,
            chunk_bytes=args.chunk_bytes,
            max_single_file_bytes=args.max_single_file_bytes,
            ignore_lock_files=ignore_lock_files,
            auto_build=args.auto_build,
            lock=threading.Lock(),
        )

    if not repos:
        print("[FATAL] no valid repos")
        raise SystemExit(2)

    # warmup build all repos (optional)
    if args.warmup:
        for repo in repos.values():
            with repo.lock:
                core.build_bundles(
                    root_dir=repo.root_dir,
                    cache_dir=repo.cache_dir,
                    chunk_bytes=repo.chunk_bytes,
                    max_single_file_bytes=repo.max_single_file_bytes,
                    ignore_lock_files=repo.ignore_lock_files,
                )

    httpd = MultiRepoServer((args.bind, args.port), Handler, repos=repos)

    print(f"[OK] MULTI REPOS: {len(repos)}")
    for name, repo in repos.items():
        print(f"  - {name}: {repo.root_dir}")
    print(f"[OK] LOCAL: http://{args.bind}:{args.port}")
    print("Endpoints: /repos /r/<repo>/all /r/<repo>/tree /r/<repo>/file?path=... /health")
    httpd.serve_forever()


if __name__ == "__main__":
    main()