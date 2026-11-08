# 受控 R2 Relay 安装准备器

目标：从已 Stage 的固定 Commit 生成 Git 归档，校验路径、文件类型和大小，再安装到 /opt/r2-relay。默认 dry-run，apply 才写入。依赖安装以 r2relay 非特权用户运行，root 不执行仓库脚本，不启动 systemd，不开放 8443/18085，不接触真实 R2。

本阶段只实现独立安装器与测试，**尚未接入现有 MCP/rootd**。后续需要审核后的 rootd 固定动作及 MCP Tool 接口，并由 Owner 对已有 rootd 进行一次可信升级。严禁通过任意命令执行来绕过现有工具白名单。安装失败保留部分文件供审核，拒绝覆盖已有 /opt/r2-relay。
