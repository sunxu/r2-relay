# Mac 本地 R2 Relay 的 launchd 版本化管理

目标：在 Apple Silicon Mac 上以**用户级 LaunchAgent**管理 `127.0.0.1:18585`，不需要 root、Docker 或改动 VPS 服务。**此功能在 Mac 实机验收前处于待验证状态。** 本地管理命令不负责更改应用的分片并发策略；部署前必须确认 Owner 实测的五并发改动已经包含在目标 SHA 中。

## 运行边界

- 仅在当前用户登录的 macOS 图形会话中运行，用户注销或 Mac 睡眠后不保证上传继续。
- `r2-relay install --sha <完整SHA> --repo <项目路径>` **只构建固定 Git commit 的独立 release、创建配置和私有 LaunchAgent 模板，绝不在 `~/Library/LaunchAgents` 发布自启配置，也不启动服务**。不碰现有 18585 进程。
- `r2-relay update <SHA> --repo <项目路径>` **只预装**；需要 `--activate --confirm-idle` 才会主动切换并重启。
- `stop/activate/restart/rollback/uninstall` 必须提供 `--confirm-idle`，由操作者确认没有在途上传。工具无法证明 VPS 上所有 R2 Worker 已完成。
- 如果 18585 被非本 LaunchAgent 管理的进程占用，拒绝启动或切换；不执行 kill -9，不回退到 2 并发。
- Release 目录 `~/.local/share/r2-relay/releases/<SHA>`，`current`/`previous` 为符号链接；保留旧版本供回滚。不会修改原始 Git checkout，拒绝含未提交改动的源仓库。必须使用完整40位 SHA，不接受 `HEAD` 或分支名。
- 先把 Git 归档解包为临时源码，再移动到最终 Release 路径，**之后**才创建 `.venv` 和安装 Python 包；保证 `mitmdump` shebang 与 editable 安装路径在更新后有效。失败删除未完成安装，但不会切换当前版本。
- CLI 管理器独立复制到 `~/.local/share/r2-relay/manage.py`，不依赖源 Git checkout；操作使用排他锁，阻止同时升级和回滚。
- 启动不仅检查端口存在，还核验监听 PID 与 `launchctl print` 记录的进程一致。
- Token 存于 `~/.config/r2-relay/client.env` (0600)，不写入 LaunchAgent plist、Git、CLI 参数或日志。使用独立 VPS CA 与 mitmproxy CA，仍严格验证 TLS。
- `stop` 会卸载 LaunchAgent 并删除登录自启 plist；即使重新登录也不会自行启动，需重新执行 `start`。`restart` 不应在真实上传中执行。

## 首次安装（Mac 终端）

要求 Python 3.11+、Git、macOS 已安装并能通过可信渠道取得 VPS CA 公共证书和本地 Token。

```bash
git clone https://github.com/sunxu/r2-relay.git "$HOME/workspace/r2-relay"
cd "$HOME/workspace/r2-relay"
# 使用已合并且包含五并发代码的完整 Commit SHA；不要直接将旧 main 当作已验证版本
SHA="填入40位小写已验证SHA"
python3.11 ops/macos/manage.py install --sha "$SHA" --repo "$PWD"
```

安装过程将生成 `~/.local/bin/r2-relay`、`~/.local/bin/r2-relay-run`、`~/.local/share/r2-relay/agent.plist`（未启用模板）。**只有显式执行 `r2-relay start` 才会发布 `~/Library/LaunchAgents/com.sunxu.r2-relay.local.plist`，因此安装后重新登录也不会意外抢占已有 18585。** 还会创建仅含占位 Token 的 `~/.config/r2-relay/client.env`；请编辑配置并放入正确的 VPS 根证书：

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

首次验证要包含实际 R2 分片成功回执、Relay 内部计时与五并发峰值。只有端口监听不意味着真实上传成功；还应核对 `r2-relay status` 输出 `port_18585_managed=True`。应用侧继续使用显式代理 `http://127.0.0.1:18585`，并在原有 CA 集合中追加 `~/.mitmproxy/mitmproxy-ca-cert.pem`，**不能**关闭证书验证。

## 日常使用

```bash
r2-relay status
r2-relay logs
r2-relay stop --confirm-idle  # 必须先确认无上传
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

卸载：`r2-relay uninstall --confirm-idle`，删除 LaunchAgent、命令入口以及 current/previous 激活链接，但不删除历史版本目录、Token、证书和日志；可重新安装。不会停止旧的非托管 18585 进程。

## 安全测试与局限

离线执行：

```bash
python3 -m unittest discover -s tests -p test_macos_manage.py -v
```

离线单元测试覆盖安装后不自动启动、停止后跨登录持续禁用、启动失败清除自启定义、固定路径 venv、失败清理、CLI 独立性、并发管理锁、监听 PID 归属、非法 SHA/链接、更新失败回滚等。测试不触达 VPS、R2、GitHub 凭据，也不会启动真实 LaunchAgent。GitHub Actions 在 Linux 上只检查跨平台纯逻辑，**不能替代 macOS 实机验证**。更新/回滚不能无损恢复正在执行的 R2 PUT，因此不可把 `--confirm-idle` 当作自动“无在途上传”的判定。若源仓库有未提交改动，管理器拒绝构建；先把现有五并发代码冻结为 Git Commit 再安装。

## 当前五并发部署特别注意

Owner 在 2026-11-15 已报告五并发 97 片真实上传全部 HTTP 200、分片阶段 48.51 秒。**这属于现有运行进程的观测结果，不足以断言 GitHub `main` 的固定 SHA 可重现五并发。** 不要直接将当前 `main` SHA 当作已冻结的五并发版本；先检查运行时配置来源、将必要代码与配置持久化，独立验收后再切换。

配置模板中的 `R2_RELAY_HTTP2=1` 只是当前项目的默认值。若现有五并发运行进程使用 HTTP/1.1 或其他协议开关，部署前应核对并复用；切换代理协议与并发配置不属于本管理器职责。

## 已知限制

- CLI 管理器在首次安装时复制为独立文件；更新代理应用 SHA 并不会自动覆盖管理器自身。管理器版本升级属于单独维护操作。
- 依赖在安装时从 Python 软件源解析；当前 `requirements/rc1-direct.txt` 仅为 Linux 观测的直接依赖版本，不是 Mac 完整 lockfile，故同一 SHA 的新安装不等于所有第三方 wheel 完全一致。
- 保持 Mac 的用户图形会话在线；睡眠和注销期间不保证持续代理。
- 若 GitHub Actions 在尚未执行任何步骤前失败，需要先排查仓库 Actions 执行权限、配额、计费或 Runner 服务，不能将此类失败记成代码测试失败或通过。