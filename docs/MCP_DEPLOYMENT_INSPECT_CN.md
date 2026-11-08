# 通过现有 MCP 增加 deployment_inspect（只读）

针对 Debian VPS：只获取固定 systemd 服务的 LoadState/ActiveState、固定部署路径的存在性/权限，以及五个端口 22/443/8443/18085/18765 的本地监听地址。

**不读取文件内容、Token、私钥、日志，不接受任何任意命令或路径参数，不做安装、重启、外部网络连接。**

## 用户授权的安装步骤（现有 MCP 尚不能执行写入）

先在本地 checkout 已合并的 GitHub main，然后通过现有 root SSH 22 上传：

~~~bash
scp -P 22 ops/deployment_inspect.py root@YOUR_VPS:/tmp/deployment_inspect.py
~~~

在 VPS 上执行：

~~~bash
install -o root -g mcp-relay -m 0640 /tmp/deployment_inspect.py /opt/r2-mcp/deployment_inspect.py
rm -f /tmp/deployment_inspect.py
~~~

在已有的 /opt/r2-mcp/server.py 文件内，找到现有的 FastMCP 实例 mcp，
**在 mcp.run() 之前**加入以下代码（不要覆盖现有 server.py）：

~~~python
from deployment_inspect import deployment_inspect as inspect_safe

@mcp.tool()
def deployment_inspect() -> dict:
    """只读查看 R2 Relay 部署状态。"""
    return inspect_safe()
~~~

检查并重启 **仅 MCP 服务**（不触碰 Caddy、Docker、R2 Relay）：

~~~bash
/opt/r2-mcp/venv/bin/python -m py_compile /opt/r2-mcp/server.py /opt/r2-mcp/deployment_inspect.py
systemctl restart r2-mcp.service
systemctl is-active r2-mcp.service
~~~

若 MCP 服务发现失败，恢复 server.py 的原备份，再重启 r2-mcp.service。
该命令需要你在 VPS 的受控 SSH 环境中运行；现有 ChatGPT MCP 工具只有只读访问，不能代你执行该安装。

新增工具连通后，可以直接在 ChatGPT 调用，判断服务是否安装（LoadState=not-found）、是否启动及监听地址。所有结果只能代表读取时状态；不能作为真实 VPS/R2 协议验收。
