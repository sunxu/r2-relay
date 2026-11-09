# R2 Relay：root-only 服务核验及 loopback 启动

vps_r2_service_verify 完全只读，检查固定 unit、systemd FragmentPath/DropInPaths、root-only 环境文件 0600、私有目录 0700、随机 Token 格式与配置数值范围、缓存目录属主和权限、443 监听、8443 未监听。响应不包含密钥。

vps_r2_loopback_start 只能在 verify PASS、service inactive 和 18085 空闲时触发固定 systemctl start r2-relay-vps.service。用 127.0.0.1:18085/healthz 检查 200 和具体 JSON，再用 ss 确认只有 127.0.0.1:18085 监听。失败时尝试 stop；不会 enable，不操作 sing-box/Caddy/443/8443，不执行真实 R2。

新工具通过 Unix Socket rootd 白名单固定动作。ChatGPT 无法传递任意路径、参数或 shell。正式上线后，按先 verify、后 start、再只读 inspection 的顺序执行。该验收不代表公网 HTTPS 或真实 R2 功能通过。

接入 VPS 需要可信 root SSH 一次安装 root-owned 升级器，原服务备份保留。GitHub 合并后从固定 SHA 下载 ops/bootstrap_r2_loopback.py，先无 --apply dry-run，再 --apply；仅重启 r2-rootd 与 r2-mcp。重新同步 bandwagonhost 工具目录。


## 当前状态补充（2026-11-09）

本文件前文的“8443 未监听”是 Caddy 部署前的准入条件，不是当前状态。Caddy 已由后续固定 IP 自签名 HTTPS 部署流程启用；当前 8443 对外监听，原有 443 保持，R2 API 仍仅监听 127.0.0.1:18085。以 `vps_r2_deployment_status` 的统一检查为准，不要在当前已部署状态下重复执行初始 preflight/prepare 流程。
