# mitmproxy 磁盘响应 PoC

此 PoC 不修改生产 addon，也不访问真实 R2 或 VPS。

## 验证内容

- 测试客户端通过 HTTP CONNECT/TLS 访问一个虚拟 R2 主机。
- PoC addon 将请求改写到 127.0.0.1 上的本地文件服务器。
- 文件服务器通过 HTTP/1.1 从磁盘分块读取稀疏文件。
- addon 在 responseheaders 阶段设置 response streaming，让 mitmproxy 将上游文件响应流式转发给原始 TLS 客户端。
- 测试分别验证客户端 HTTP/1.1 和 HTTP/2、Content-Length、完整字节数，以及 mitmdump 进程 RSS 增量。

这验证的是 Loopback 文件响应源方案，不是让 mitmproxy 直接把任意本地文件绑定到合成 response.stream。

## 运行

从仓库根目录：

    python -m pip install -e './local[test]'
    R2_POC_SIZE_MIB=1024 python -m pytest -q local/tests/test_disk_response_poc.py

默认文件大小为 64 MiB；CI 将 R2_POC_SIZE_MIB 设置为 1024，执行 1 GiB 的 HTTP/1.1 和 HTTP/2 两组测试。

PoC 只绑定 127.0.0.1，使用临时 mitmproxy CA，测试结束会关闭代理和文件服务器。不会改动现有代理配置、VPS 或真实 R2。
