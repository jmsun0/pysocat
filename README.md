# pysocat

socat的python实现，同时拓展了一些功能

## 安装
------
### pip安装
```bash
pip3 install https://github.com/jmsun0/pysocat/releases/download/0.0.1/pysocat-0.0.1-py3-none-any.whl
```
### 打包的二进制执行文件（ubuntu-20以上）
```bash
curl -sSfL https://github.com/jmsun0/pysocat/releases/download/0.0.1/pysocat-0.0.1.tar.gz | tar xzf - -C /bin/
```
## 使用
------
### 基本用法示例（参考socat）
```bash
pysocat tcp-l:1111,reuseaddr -
pysocat tcp:127.0.0.1:1111 -
pysocat tcp:127.0.0.1:9080 tcp-l:2222,reuseaddr,fork
pysocat udp-l:1111 -
pysocat udp:127.0.0.1:1111 -
pysocat unix-l:/tmp/test.sock -
pysocat unix:/tmp/test.sock -
pysocat system:'ls -lh /proc/self/fd',fdin=29,fdout=30 -
pysocat system:'bash <&29 >&30',fdin=29,fdout=30 -
pysocat exec:bash -
pysocat tcp-l:1111,reuseaddr tun:192.168.7.1/24
pysocat tcp:127.0.0.1:1111 tun:192.168.7.2/24
```
### 拓展用法
```bash
# http chunked
pysocat http-l:1111 -
pysocat http://127.0.0.1:1111 -
pysocat https://xxx -
# websocket
pysocat ws-l:1111 -
pysocat ws://127.0.0.1:1111 -
pysocat wss://xxx -
# 启动 http 中继服务
pysocat http-relay:1111,path_prefix=""
# 分别启动两客户端通过中继服务建立管道实现相互聊天（aaa是管道名，同名管道相互配对，http和ws数据互通）
pysocat http://127.0.0.1:1111/pipe/aaa -
pysocat ws://127.0.0.1:1111/pipe/aaa -
# 通过中继服务转发端口（只能连接一次，访问机器2的2222端口相当于访问机器1的9080端口）
pysocat http://127.0.0.1:1111/pipe/aaa tcp:127.0.0.1:9080
pysocat http://127.0.0.1:1111/pipe/aaa tcp-l:2222,reuseaddr
# 通过中继服务转发端口（持久转发，split表示采用特殊的协议拆分管道使得能够重复使用）
pysocat http://127.0.0.1:1111/pipe/aaa,split tcp:127.0.0.1:9080
pysocat http://127.0.0.1:1111/pipe/aaa,split tcp-l:2222,reuseaddr,fork
# 持久转发端口另一种方法
pysocat tcp-l:1111,reuseaddr,split tcp:127.0.0.1:9080
pysocat tcp:127.0.0.1:1111,split tcp-l:2222,reuseaddr,fork
# 通过中继服务建立虚拟局域网
pysocat http://127.0.0.1:1111/pipe/aaa tun:192.168.7.1/24
pysocat http://127.0.0.1:1111/pipe/aaa tun:192.168.7.2/24
# 通过中继反向代理本地web服务（相当于内网穿透功能）
pysocat tcp:127.0.0.1:9080 http://127.0.0.1:1111/push/xxx,split
curl http://127.0.0.1:1111/access/xxx
```

