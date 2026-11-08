# 通用 VPS MCP v1 接入说明

## 当前能力
保留旧五个工具，新增 vps_system_snapshot（固定系统只读命令）、vps_project_doc_read（固定项目公开文档）和 vps_git_stage（只 Stage 白名单 Git 仓库的 40 位完整 SHA）。新增工具全部通过原有 Secure MCP Tunnel。项目 allowlist 当前仅包括 r2-relay；不是任意 Git URL 执行器。

## 安全边界
MCP 层仍保持 NoNewPrivileges=yes；不开放 shell/root 任意命令、Docker 管理、系统文件写入或联网 R2 上传。Stage 写入在 mcp-relay 自己的 /var/lib/vps-mcp/workspaces 内，不执行仓库代码、pip install、systemctl。下载仓库不等于业务部署。项目文档读取仅限 README.md 与 docs 内小文本文件；禁止绝对路径、遍历、symlink。实际生产部署另设审查阶段。

## 首次 VPS 集成步骤（仅在 PR 通过测试并合并后）
1. 使用固定已审核 Commit 下载 ops/generic_vps_mcp.py，安装为 /opt/r2-mcp/generic_vps_mcp.py，归属 root:mcp-relay，权限 0640。
2. 创建 /var/lib/vps-mcp 和 /var/lib/vps-mcp/workspaces，归属 mcp-relay:mcp-relay，权限 0700。
3. 备份 /opt/r2-mcp/server.py。在已有 FastMCP 实例创建之后、mcp.run 调用之前加入：

    from generic_vps_mcp import register as register_generic_vps
    register_generic_vps(mcp)

4. 通过 py_compile 检查，重启 r2-mcp.service，必要时再重启 Tunnel，验证旧五个工具与新增三个工具。
5. 回滚时恢复 server.py 备份、重启 MCP/Tunnel；Stage 不触碰 sing-box/443。

## 阶段判定
此阶段代码 + CI 完成不等于 VPS 安装。下一阶段在实际 VPS Stage 成功后设计经审查的 promote/install 操作，满足 ChatGPT 直接安装 R2 Relay 的完整目标。
