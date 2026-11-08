# 无域名 HTTPS 8443：使用 Caddy internal CA 与公网 IP

**身份**：访问地址固定为 `https://<VPS 公网 IPv4>:8443`；不申请域名、不使用公开 CA。证书必须包含该 IPv4 的 `subjectAltName: IP Address`；仅有 Common Name 不够。

**推荐方式**：Caddy `tls internal`。由 Caddy 生成和轮换 CA 签发的服务器证书，避免手动管理长期叶子私钥。客户端必须安装/显式指定 Caddy **根 CA 公钥证书**，不会自动信任公网自签证书。Caddy 根 CA 私钥始终保留在 VPS，不能经 MCP、GitHub 或公网提供下载。

模板：`deploy/Caddyfile.ip-selfsigned.example`，`R2_RELAY_IP` 为明确的公网 IPv4。该模板 `auto_https disable_redirects`，只监听 HTTPS 8443，不创建 80/443 重定向，后端只连接 `127.0.0.1:18085`。

## 安全部署流程（尚未执行）
1. 确认 VPS 当前公网 IPv4 稳定；核对 Caddy 中的 IP 地址与客户端访问的 IP 一致。如果更换 IP，需重新签发并更新访问地址。
2. 受控安装 Caddy，检查版本和安装包来源；先静态验证专用 Caddyfile 的解析配置，确认不存在 80/443 监听及已有其他服务配置冲突。
3. Caddy 独立用户管理 CA 私钥和存储目录，确保私钥不可读给 `mcp-relay` 和其他非特权账户；**绝不能复制 CA 私钥给客户端**。
4. 在用户明确批准后，仅启动 Caddy 8443；核对 443 的监听进程未变化且 18085 依然只监听 loopback。
5. 在 Caddy data directory 找到根 CA **公钥证书**（通常为 `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`）；先通过可信通道传给 Mac，并独立比对 SHA256 指纹，客户端使用该 CA PEM 校验（证书链 + IP SAN），绝不使用 `verify=False` 或 `curl -k` 作正式验收。
6. 验证 `https://IP:8443/healthz` 返回 200；验证未带 Token 的 `/v1/metrics` 返回 401。生产 Token 在 VPS 生成且此前未外发，应设计独立的安全同步机制，不将其写入聊天/日志。
7. HTTPS 验收通过后才进行受控 R2 上传与 HIT/MISS 测试。HTTP 200 /healthz 不代表真实 R2 或传输性能通过。

## 客户端示例
```python
import httpx
client = httpx.Client(http2=True, verify="/path/to/caddy-root-ca.crt")
response = client.get("https://VPS_PUBLIC_IP:8443/healthz")
response.raise_for_status()
```

如果 VPS 公网 IP 未知，**不要猜测**或使用本机私有 IP 作为证书身份，待确认后才允许启动 Caddy。

参考：[Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https)、[Caddy tls internal](https://caddyserver.com/docs/caddyfile/directives/tls)、[Caddy running](https://caddyserver.com/docs/running)。
