# thordata-llm-code-share

把本地代码仓库暴露为 **大模型友好的只读文本接口**（本地安全模式 / 可选 Cloudflare Quick Tunnel 公网分享）。

这个工具的目标是把“给大模型喂代码”变成一个稳定、可复用的流程：

1. 启动本地只读文本服务（带过滤规则）
2. 可选：构建缓存，把仓库切成多个 chunk（减少超时/提升读取稳定性）
3. 可选：启动 Cloudflare Quick Tunnel，拿到公网 URL 分享给远程大模型
4. 直接复制工具输出的提示词模板，让模型按 `/all` → `/all?part=N` 或 `/tree` + `/file` 读取

---

## 包含哪些文件

- `llm_server.py`：把仓库暴露成只读文本接口的 HTTP Server
- `start_quick_tunnel.py`：一键启动 Server + `cloudflared`（Quick Tunnel），打印可分享链接与提示词模板

---

## 核心特性

### 全量通读（给大模型一次性扫仓库）
- `/build` 把仓库打包成多个 chunk 写入缓存目录（默认 `.llm_cache/`）
- `/all` 返回 **秒回索引**（FAST index）
- `/all?part=N` 返回指定 chunk（从缓存文件快速流式输出）

### 精准读取（更推荐，仓库大时更省 token）
- `/tree` 输出过滤后的文件清单 + 大小
- `/file?path=...` 精准读取单文件（纯文本 + 头信息）

### 默认更安全
- 默认只绑定 `127.0.0.1`（只允许本机访问）
- 不开放目录浏览，只开放明确的几个接口
- 默认屏蔽常见敏感文件/二进制后缀（如 `.env/.pem/.key` 等）
- 默认忽略依赖/构建输出目录（如 `node_modules/dist/target/vendor/...`）
- 二进制探测：包含 `\x00` 直接跳过
- 单文件过大自动截断，避免撑爆 chunk/导致超时

---

## 环境要求

- Python 3（建议 3.9+）
- 可选：`cloudflared`（只在你需要公网分享链接时才需要）

---

## 快速开始：仅本地使用

在 **thordata-llm-code-share** 仓库目录下执行：

```bash
python llm_server.py --root "/path/to/your/repo" --warmup
```

常用地址：

- `http://127.0.0.1:8080/health`
- `http://127.0.0.1:8080/all`（索引）
- `http://127.0.0.1:8080/all?part=1`（第 1 片）
- `http://127.0.0.1:8080/tree`（文件列表）
- `http://127.0.0.1:8080/file?path=README.md`（读单文件）

---

## 快速开始：公网分享（Cloudflare Quick Tunnel）

> 这个命令会同时启动 `llm_server.py` 和 `cloudflared`，然后在终端打印公网 URL、分片链接、以及可直接复制给大模型的提示词模板。

```bash
python start_quick_tunnel.py --root "/path/to/your/repo" --chunk-bytes 600000 --auto-port
```

你会得到：
- 公网域名：`https://xxxx.trycloudflare.com`
- `/all`、`/tree`、`/all?part=1..N`、`/file?path=...`
- 两套提示词模板（全量通读 / 精准读文件）

### 网络需要 HTTP 代理时（例如 Clash）
```bash
python start_quick_tunnel.py \
  --root "/path/to/your/repo" \
  --chunk-bytes 600000 \
  --auto-port \
  --proxy "http://127.0.0.1:7897"
```

该参数会把代理环境变量传给 `cloudflared`。

---

## 接口说明

### `GET /`
返回简短帮助信息。

### `GET /health`
健康检查，返回 `ok`。

### `GET /robots.txt`
返回 `Disallow: /`，减少爬虫/探测器噪音。

### `GET /tree`
返回 TSV（制表符分隔）：

- `相对路径<TAB>文件大小字节数`

输出会经过过滤规则。

### `GET /file?path=relative/path/to/file.py`
读取单文件（纯文本 + 文件头）。会做安全校验：
- path 不能逃逸出 `--root`
- 命中敏感/二进制/忽略规则会直接拒绝
- 跳过 symlink

### `GET /build[?refresh=1]`
构建缓存分片（重任务）。返回 `meta.json`（JSON）。

### `GET /meta`
读取缓存 `meta.json`（必须先有缓存）。

### `GET /all`
如果已有缓存：返回 `index.txt`（秒回）。
如果没有缓存：提示先运行 `/build`。

### `GET /all?part=N`
返回第 N 片 bundle 内容（从缓存文件快速输出）。

---

## 参数说明（CLI）

### `llm_server.py`
常用参数：

- `--root`：仓库根目录（默认 cwd）
- `--bind`：监听地址（默认 `127.0.0.1`）
- `--port`：端口（默认 `8080`）
- `--warmup`：启动时先 build 一次（推荐）
- `--auto-build`：首次访问 `/all` 时若无缓存就自动 build
- `--chunk-bytes`：chunk 大小（建议 60 万 ~ 120 万）
- `--max-single-file-bytes`：单文件最大读取字节数，超出截断（默认 300 万）
- `--cache-dirname`：缓存目录名（默认 `.llm_cache`）
- `--no-lock-ignore`：不忽略 `*.lock` 文件
- `--exclude-github`：忽略 `.github`

### `start_quick_tunnel.py`
常用参数：

- `--root`：仓库根目录（必填）
- `--auto-port`：端口被占用时自动找下一个可用端口
- `--chunk-bytes`：推荐 `600000`（更稳）
- `--protocol`：`http2` 或 `quic`（默认 `http2`）
- `--proxy`：cloudflared 使用的代理（如 `http://127.0.0.1:7897`）
- `--open`：自动用浏览器打开公网 `/all`
- `--no-warmup`：跳过 warmup build（不推荐）
- `--auto-build`：server 侧缺缓存时自动 build

---

## 推荐给大模型的阅读方式

### 方式 A：全量通读（最省事）
1. 先读 `/all`（索引）
2. 再按顺序读 `/all?part=1..N`
3. 引用代码时使用 bundle 里的 `FILE: ...` 路径

### 方式 B：精准读取（仓库大时更省 token）
1. 先读 `/tree`
2. 按需用 `/file?path=...` 精准读取
3. 需要大范围上下文时再补 `/all`

---

## 常见问题

### 公网链接出现 Cloudflare 1033
通常是 `cloudflared` 不健康或已退出。请确保启动脚本所在终端持续运行，并观察 cloudflared 日志是否还在输出。

### 大模型读取慢/超时、分片太多
调整 `--chunk-bytes`：
- `600000`：更稳，但分片更多
- `1200000`：分片更少，但某些 fetcher 更容易超时

---

## License
MIT