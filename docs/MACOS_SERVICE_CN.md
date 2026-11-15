# Mac 本地 R2 Relay 的 launchd 版本化管理

目标：在 Apple Silicon Mac 上以**用户级 LaunchAgent**管理 `127.0.0.1:18585`，不需要 root、Docker 或改动 VPS 服务。**此功能在 Mac 实机验收前处于待验证状态。** 本地管理命令不负责更改应用的分片并发策略；部署前必须确认 Owner 实测的五并发改动已经包含在目标 SHA 中。

## 运行边界

- 仅在当前用户登录的 macOS 图形会话中运行，用户注销或 Mac 睡眠后不保证上传继续。
- `r2-relay install` **只构建固定 Git commit 的独立 release、创建配置模板及 plist，绝不启动服务**。不碰现有 18585 进程。
- `r2-relay update <SHA>` **只预装**；需要 `--activate --confirm-idle` 才会主动切换并重启。
- `activate/restart/rollback/uninstall` 必须提供 `--confirm-idle`，由操作者确认没有在途上传。工具无法证明 VPS 上所有 R2 Worker 已完成。
- 如果 18585 被非本 LaunchAgent 管理的进程占用，拒绝启动或切换；不执行 kill -9，不回退到 2 并发。
- Release 目录 `~/.local/share/r2-relay/releases/<SHA>`，`current`/`previous` 为符号链接；保留旧版本供回滚。不会修改原始 Git checkout，拒绝含未提交改动的源仓库。
- Token 存于 `~/.config/r2-relay/client.env` (0600)，不写入 LaunchAgent plist、Git、CLI 参数或日志。使用独立 VPS CA 与 mitmproxy CA，仍严格验证 TLS。
- `stop` 会卸载 LaunchAgent；重新启动需 `start`。`restart` 不应在真实上传中执行。

## 首次安装（Mac 终端）

要求 Python 3.11+、Git、macOS 已安装并能通过可信渠道取得 VPS CA 公共证书和本地 Token。

```bash
git clone https://github.com/sunxu/r2-relay.git "$HOME/workspace/r2-relay"
cd "$HOME/workspace/r2-relay"
# 使用已合并且包含五并发代码的完整 Commit SHA；不要直接将旧 main 当作已验证版本
SHA="填入40位小写已验证SHA"
python3.11 ops/macos/manage.py install --sha "$SHA" --repo "$PWD"
```

安装过程将生成 `~/.local/bin/r2-relay`、`~/.local/bin/r2-relay-run`、`~/Library/LaunchAgents/com.sunxu.r2-relay.local.plist`。还会创建仅含占位 Token 的 `~/.config/r2-relay/client.env`；请编辑配置并放入正确的 VPS 根证书：

```bash
chmod 700 "$HOME/.config/r2-relay"
chmod 600 "$HOME/.config/r2-relay/client.env"
# 从可信通道获取公共证书，核对独立来源记录的 SHA-256
shasum -a 256 "$HOME/.config/r2-relay/root.crt"
"$HOME/.local/bin/r2-relay" status
```

**首次启用前**：先停止旧手工运行的 18585 实例，确保没有正在进行的分片上传，再运行：

```bash
"$HOME/.local/bin/r2-relay" start
lsof -nP -iTCP:18585 -sTCP:LISTEN
"$HOME/.local/bin/r2-relay" logs
```

首次验证要包含实际 R2 分片成功回执、Relay 内部计时与五并发峰值。只有端口监听不意味着真实上传成功。应用侧继续使用显式代理 `http://127.0.0.1:18585`，并在原有 CA 集合中追加 `~/.mitmproxy/mitmproxy-ca-cert.pem`，**不能**关闭证书验证。

## 日常使用

```bash
r2-relay status
r2-relay logs
r2-relay stop                 # 先确认无上传
r2-relay start
r2-relay restart --confirm-idle
```

需要将 `~/.local/bin` 放入 shell 的 PATH，或继续使用完整路径。日志保留在 `~/Library/Logs/r2-relay/`；日志可能包含请求元信息，不要上传原始日志和真实预签名 URL。长期运行应观察日志体积并在维护窗口实施轮换。

## 无覆盖升级与回滚

```bash
cd "$HOME/workspace/r2-relay"
git fetch origin
# 先人工确认代码、CI 和目标 SHA，特别是五并发代码已被提交
r2-relay update <40位目标SHA> --repo "$PWD"  # 只预安装，不打断传输
r2-relay status
# 确保 IOTA 已结束、无活动 UploadPart，再允许切换
r2-relay activate <40位目标SHA> --confirm-idle
# 如新服务启动/监听检查失败，脚本尝试回到先前版本；仍需人工检查日志
r2-relay rollback --confirm-idle
```

也可一次执行 `r2-relay update <SHA> --activate --confirm-idle`，但不推荐未经预检查直接用于生产。验证之后才能清理旧版本目录；管理器默认保留所有 release。

卸载：`r2-relay uninstall --confirm-idle`，只删除 LaunchAgent 与命令入口，不删除版本、Token、证书和日志。不会停止旧的非托管 18585 进程。

## 安全测试与局限

离线执行：

```bash
python3 -m unittest discover -s tests -p test_macos_manage.py -v
```

测试不触达 VPS、R2、GitHub 凭据，也不会启动真实 LaunchAgent。GitHub Actions 在 Linux 上只检查跨平台纯逻辑，**不能替代 macOS 实机验证**。更新/回滚不能无损恢复正在执行的 R2 PUT，因此不可把 `--confirm-idle` 当作自动“无在途上传”的判定。若源仓库有未提交改动，管理器拒绝构建；先把现有五并发代码冻结为 Git Commit 再安装。
