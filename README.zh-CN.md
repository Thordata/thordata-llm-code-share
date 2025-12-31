# thordata-llm-code-share
一个小而实用的工具：把本地仓库变成 **只读、适合大模型读取的文本接口**。

支持两种读取模式：
- **全量通读模式**：`/all`（秒回索引）→ `/all?part=N`（分片 bundle）
- **精准读取模式**：`/tree` → `/file?path=...`（按需读文件）

默认本机安全（仅 127.0.0.1）。可选 Cloudflare Quick Tunnel 一键生成公网链接分享给远程模型。


---

## 问题是什么
大模型想“读懂仓库”经常会被现实限制卡住：
- 仓库太大，塞不进聊天上下文
- 手动复制粘贴慢、容易漏文件
- 大模型 URL 抓取经常遇到超时/中断
- 你希望模型读取的是你本机当前代码（而不是你粘贴的一小段）

---

## 解决方案与工作原理
这个工具提供一个极简 HTTP Server，把仓库作为纯文本输出：

### 模式 1：全量通读（分片 bundle）
1）`GET /build` 扫描仓库，生成分片缓存写入 `.llm_cache/`  
2）`GET /all` 返回很小的索引（秒回）  
3）`GET /all?part=N` 返回第 N 片纯文本（从磁盘流式输出）

为什么要分片：
- 很多大模型“读 URL”的抓取器有严格超时
- 分片能显著降低“单个超大响应失败”的概率

### 模式 2：精准读取（更可控）
- `GET /tree` 输出过滤后的文件清单
- `GET /file?path=...` 精准读取单文件

仓库越大，精准模式越省 token、越不容易“读一堆无关文件”。

---

## 适用场景
### 特别适合
- “请通读仓库并总结架构/模块边界”
- “这个 bug 需要跨多个文件定位”
- “做一次 code review（性能/安全/可维护性）”
- “生成文档/接口说明/迁移指南”
- “让 Agent 用 URL 拉文件，而不是让人粘贴”

### 不适合
- 不要把 Quick Tunnel 当作生产级服务（它是临时分享用的）
- 不要在不确认过滤规则的情况下把公网链接发给不可信对象

---

## 快速开始

### 方式 A：公网分享（Cloudflare Quick Tunnel）
1）安装 `cloudflared` 并确保 PATH 可用  
2）运行：

```bash
python start_quick_tunnel.py --root "/path/to/your/repo" --chunk-bytes 600000 --auto-port
```

脚本会打印：
- 公网域名 `https://xxxx.trycloudflare.com`
- `/all`（从这里开始）、`/tree`、`/all?part=1..N`、`/file?path=...`
- 两套提示词模板（全量通读 + 精准读文件）

如果网络需要代理：
```bash
python start_quick_tunnel.py --root "/path/to/your/repo" --chunk-bytes 600000 --auto-port --proxy "http://127.0.0.1:7897"
```

### 方式 B：仅本地
```bash
python llm_server.py --root "/path/to/your/repo" --warmup
```

---

## 接口说明
- `GET /`：简短帮助
- `GET /health`：健康检查，返回 `ok`
- `GET /robots.txt`：返回 `Disallow: /`（减少探测噪音）
- `GET /tree`：过滤后的文件清单（TSV：相对路径 + 大小）
- `GET /file?path=...`：读取单文件（会做防逃逸/过滤/二进制检测）
- `GET /build[?refresh=1]`：构建分片缓存并返回 meta
- `GET /meta`：读取 meta（需先 build）
- `GET /all`：读取索引（秒回）
- `GET /all?part=N`：读取第 N 片 bundle

---

## 调参指南（超时 vs 分片数）
关键参数：
- `--chunk-bytes`：分片大小
- `--max-single-file-bytes`：单文件超大时截断

推荐：
- `600000`：更稳（更不容易超时），但分片更多
- `900000`：折中
- `1200000`：分片更少，但部分抓取器更容易超时

如果模型经常拉取失败：
- 调小 `--chunk-bytes`
- 排除噪音文件（如 lockfiles、生成物、大 JSON 等）

---

## 安全策略
默认更安全的行为：
- 默认仅监听 `127.0.0.1`
- 不开放目录浏览（只开放固定接口）
- 屏蔽常见敏感文件名/后缀（`.env/.pem/.key` 等）
- 忽略常见依赖/构建目录
- 跳过 symlink 和二进制文件（含 0x00）
- 大文件截断降低风险

注意：这是“降低风险”，不是“绝对保证”。公网分享前请自行评估过滤规则覆盖范围。

---

## 限制与边界
- Quick Tunnel 不保证在线；必须保持脚本运行，且 URL 每次可能变化。
- 忽略规则是启发式的；不同公司/项目需要按需调整。
- 大模型抓取器各不相同：有的超时严格、有的缓存强、有的并发策略不同。

---

## 常见问题排查

### 公网链接出现 Cloudflare 1033
通常是 cloudflared 不健康或已退出。保持启动脚本所在终端运行，并观察 cloudflared 日志。

### 公司 Wi‑Fi 下打不开，但手机 4G/5G 可以打开
这通常是公司网络对 `*.trycloudflare.com` 做了限制或干扰（DNS 污染、Cloudflare 边缘 IP 封锁、TLS/代理审计等）。

建议：
- 如果使用 Clash 等代理工具，请把 `trycloudflare.com` 强制走可用代理策略（不要走 DIRECT）。
- 如遇 DNS 污染，可开启 Clash DNS / TUN。
- 推荐使用 `--public-check warn`：本机公司网打不开 **不代表** 外网/LLM 无法访问。

快速验证：
- 手机 4G/5G 打开：`https://<your>.trycloudflare.com/health`
- 或在非公司网络环境测试。

### 本地 OK，但公网 /health 超时
可尝试：
- 在受限网络里使用 `--proxy`
- 切换 `--protocol http2` / `--protocol quic`
- 调小 `--chunk-bytes`

### 分片太多
适当增大 `--chunk-bytes`，或排除噪音目录/文件。

---

## License
MIT