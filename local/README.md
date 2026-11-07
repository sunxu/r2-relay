# 本地 Python HTTPS 代理 — M2 首版（未生产验收）

采用 Python mitmproxy addon 运行显式 HTTP 代理（`127.0.0.1:18585`）。mitmproxy 处理 CONNECT、TLS、按 R2 目标签发证书；自主开发的 Python addon 完整读取 UploadPart 请求体，计算 MD5、通过 HTTPS:8443 协商缓存并将真实 R2 响应交给原始客户端。

## 开发环境启动
```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e './local[test]'
export R2_RELAY_VPS_URL=https://YOUR_VPS_HOST:8443
export R2_RELAY_TOKEN='replace-with-secret'
mitmdump --mode regular --listen-host 127.0.0.1 --listen-port 18585 \
  --allow-hosts '.*\.r2\.cloudflarestorage\.com(:443)?$' \
  --set ssl_insecure=false -s local/addon_entry.py
```

  --allow-hosts '.*\.r2\.cloudflarestorage\.com(:443)?$' \

代理初次启动生成本地 CA（通常为 `~/.mitmproxy/mitmproxy-ca-cert.pem`）。原始 aiohttp 程序启动时合并旧 CA 与代理 CA，设置代理参数 `proxy="http://127.0.0.1:18585"`。不要设置 `ssl=False`；不能将代理 CA 私钥或真实签名 URL 提交 GitHub。

本地对 Content-Length 已知且不超过 32MiB 的目标请求在默认 mitmproxy 缓冲模式下完整读取，再计算 MD5。请求体过大或未知长度时在 requestheaders 阶段选择直传。执行结果中的真实 R2 502 会透传，不作为 VPS 不可用处理；VPS 连接故障由 mitmproxy 继续转发原始请求。

## 现阶段仍缺少
全局内存预算已有声明长度预留机制，但尚需异常与真实高并发压力验证；真实 aiohttp+MITM CA 已完成隔离 CI 测试，真实 VPS+R2 端到端联调仍未完成；并发与多种签名头验收；代理取消及连接异常语义；生产部署。M2 PR 基于未合并的 M1 Draft PR，请保持 Draft。
