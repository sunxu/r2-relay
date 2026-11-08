# R2 Relay RC1 离线候选版审计

日期：2026-11-08  
范围：`feat/m2-local-proxy-v01` / Draft PR #14（堆叠于 Draft PR #13）。  
结论：**离线候选版，非生产发布批准**。RC1 Gate 必须对最终 Commit 完成 SUCCESS 后，才可标记本轮离线检查通过。

## 审查结论与证据

| ID | 优先级 | 检查项 | 处理 |
|---|---|---|---|
| RC1-01 | P0 | 同 MD5 首次并发写入可能覆盖首次完整缓存、刷新固定 TTL | 已改为同目录临时文件 + 原子 `os.link` 首写获胜；后写不覆盖、不延寿。测试 `test_rc1_concurrent_cache.py` |
| RC1-02 | P0 | 缓存已存在时写入分支可能提前退出，未消费 HTTP 请求正文 | 已改为消费与校验完整声明长度，测试同上 |
| RC1-03 | P0 | 带 Content-Encoding 的 R2 错误响应可能在 HTTPX/mitmproxy 链上解码或重复编码 | Worker 优先流式读取 wire bytes，mitmproxy 先设置原始正文再附加 Content-Encoding；测试 `test_rc1_wire_encoding.py` |
| RC1-04 | P0 | 请求取消时不完整临时文件可误命中 | `finally` 清理暂存文件；取消测试 `test_rc1_cancel.py` |
| RC1-05 | P0 | VPS 每个执行记录最多保留 8MiB 响应体，累积任务可能耗尽 RAM | 新增保留响应总预算，默认 64MiB，并在终态 TTL 清理时回收；溢出返回确定的代理内部错误，不伪称 R2 成功 |
| RC1-06 | P0 | VPS 结果已不确定时重复 R2 PUT | 只有明确 TCP 尚未建连或明确 507 尚未启动 Worker 才直传；其他中断标记 UNKNOWN，不伪造 R2 成功 |
| RC1-07 | P0 | 原始 SigV4 头、URL 编码、ETag 与代理行为 | 已针对模拟签名 URL、Host、Content-Type、Content-Length 和真实 aiohttp CONNECT/TLS 测试；真实 R2 校验仍待环境 |
| RC1-08 | P1 | 离线开发测试复现性 | 新增 `scripts/rc1_offline.py` 单命令全测试 + 基准；`requirements/rc1-direct.txt` 固定直接依赖，CI 上传 Linux Python3.11 实际依赖列表 |
| RC1-09 | P1 | VPS 部署和回滚 | 提供 systemd、Caddy HTTPS 8443 与回滚说明；尚未实机演练 |
| RC1-10 | P1 | 外部上传不会被模拟误触发 | RC1 Gate 只使用本地 HTTPS 测试服务和 `httpx.MockTransport`；无真实 R2 地址的可访问测试执行 |

## 单命令离线复验

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements/rc1-direct.txt
python -m pip install -e ./vps -e ./local
python scripts/rc1_offline.py
```

该命令会依次执行 VPS / Local / Benchmarks pytest，以及 100 × 10MiB、并发 8 的本地 MockTransport 基准。真实 mitmdump 和本地 HTTPS VPS 由 pytest fixture 启停，无需外部 VPS 或 R2。快速烟测：`python scripts/rc1_offline.py --quick`。

严格版本范围：直接依赖已固定，**跨平台完整传递依赖未作 hash-lock**；RC1 Actions 生成并上传 `rc1-linux-py311-dependencies`，仅可视为 Linux Runner 的实际环境快照，不能代替未来 macOS/ARM64 锁文件。

## 未解除的安全/生产阻断

1. 未真实验证 Cloudflare R2 SigV4 的所有签名边界、ETag、Multipart Complete、URL 过期。
2. 未实测公网 VPS Caddy TLS/HTTP2 `:8443`、公网路由、防火墙与网络断连。
3. 未实测 100 Mbps 上行、1000 Mbps VPS 出口和真实应用的内存峰值；本地 512MiB 是请求体预留预算，**不是 Python 进程总 RSS 硬限制**。
4. R2 响应上限 8MiB；超限按内部错误拒绝，不能宣称任意大小响应完全透传。
5. MD5 唯一内容缓存键、不校验 VPS 接收端二次 MD5、首次相同 MD5 不做互斥是已批准取舍；并非防碰撞/防污染系统。
6. 单 VPS Worker、进程内执行记录，重启只恢复缓存文件，不恢复进行中的任务。
7. 现有 Draft PR #13 尚未包含 PR #14 的 RC1 修补。**不能单独把旧 M1 快照视为 RC1 安全版本；合并前必须整理修复进入 main 的顺序**。

## 最终审批条件

- GitHub RC1 offline gate、Local/VPS tests、Offline benchmark smoke、PR governance 全部在**相同最终 Commit** PASS；
- 真实环境阻断明确标记 `NOT_TESTED`，不是 `PASS`；
- 由仓库 Owner 决定合并/发布；当前不部署、不合并。
