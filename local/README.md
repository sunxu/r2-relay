# 本地 Python 显式 HTTPS 代理 — M2 开发版本

本地代理使用 **Python mitmproxy addon** 实现，项目自有的 Python 模块负责 UploadPart 识别、读取完整请求体、MD5、VPS Prepare/MISS/HIT 和真实 R2 响应还原。Mitmproxy 负责 CONNECT、TLS MITM、SAN 证书、其它流量正常转发。

## 开发启动（仅本机）
```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e './local[test]'
export R2_RELAY_ENABLE_ADDON=1
export R2_RELAY_VPS_URL=https://YOUR_VPS_HOST:8443
export R2_RELAY_TOKEN='replace-with-secret'
mitmdump --mode regular --listen-host 127.0.0.1 --listen-port 18585 \
  --set ssl_insecure=false --set stream_large_bodies= \
  -s local/r2relay_local/addon.py
```
注意：直接加载包内 addon.py 可能导致相对导入失败；推荐正式启动改用根目录的 `local/addon_entry.py`。实际运行命令见下。

```bash
mitmdump --mode regular --listen-host 127.0.0.1 --listen-port 18585 \
  --set ssl_insecure=false -s local/addon_entry.py
```

先启动代理生成其 CA（通常 `~/.mitmproxy/mitmproxy-ca-cert.pem`），调用方启动时将这个 CA 与原有 CA 合并，并在 aiohttp 客户端设置 `proxy="http://127.0.0.1:18585"`。不要用 `ssl=False`。私钥及真实 token 不能提交到仓库。

当请求是 R2 UploadPart 且 Content-Length 已知不超过 32 MiB 时，在 mitmproxy 默认请求缓存模式下完整读取正文后才计算 MD5。其它请求不执行缓存协商。超限或不确定长度的分片在 requestheaders 阶段启用直接流式转发。

**尚未达到生产验收**：全局缓冲内存预算、代理重启/取消语义、真实 aiohttp + TLS + VPS:8443 + R2 端到端联调及大并发压力测试均未完成。此分支基于 M1 Draft PR，需按依赖顺序评审。
