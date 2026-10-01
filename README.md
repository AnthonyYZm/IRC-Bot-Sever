# DI31001 Coursework — IRC Bot (client part)

Pure-standard-library Python IRC bot that connects over **IPv6** to the group's
server (running on the Ubuntu VM) and implements the "bot" requirements from
**Project Brief, Section A.2**.

## background knowledge

**1. Socket（套接字）是什么？**
你可以把 Socket 想象成一部**电话机**。程序想跟另一台机器上的程序“打电话”聊天，就需要先拿起这部电话机（创建 Socket），然后拨号连接（指定对方的 IP 地址和端口号）。一旦接通，双方就可以通过这部电话互相收发消息了。在 Python 里，操作系统已经为你准备好了这部“电话机”，直接用 `socket` 模块就能用，不用管不同系统的差异。

**2. IRC 协议是什么？**
IRC 是 **Internet Relay Chat** 的缩写，是一种很老的**文本聊天协议**。它的工作方式很简单：你需要先连到一个**中央服务器**，所有的聊天消息都通过这个服务器转发。你可以跟所有人一起在**频道**（Channel，比如 `#test`）里聊天，也可以跟某个人**私聊**。这个项目要做的，就是实现一个能自动聊天的机器人（客户端）和一个简化的中央服务器。

**3. 你提到的 IPv6 和 TCP 是什么？**
*   **TCP**：是“传输控制协议”，它负责**可靠地**把数据送到目的地。IRC 之所以跑在 TCP 上，就是因为聊天消息不能丢，必须保证对方能收到。
*   **IPv6**：是新一代的互联网地址协议。现在常用的 IPv4 地址快用完了，IPv6 地址数量多得多。你的项目要求用 IPv6 连接，所以配置里会看到 `fc00:1337::19` 这种很长的地址，而不是常见的 `192.168.x.x`。

**4. 为什么文档要强调“客户端”和“服务器”？**
因为网络编程的核心就是这两种角色的互动：
*   **服务器（Server）**：像总机接线员，一直**等待**别人打进来。它负责接听、记录谁在线、把消息转发给正确的人。
*   **客户端（Client）**：像你打电话的人。它主动**发起**连接，连上服务器后，通过发送特定格式的文字消息来聊天或执行命令。

**5. 文档里反复提的“RFC”是什么？**
RFC（Request for Comments）就是**技术标准的说明书**。互联网上的协议（比如 HTTP、IRC）都是按 RFC 文档来定义的。你项目里提到的 RFC 1459、RFC 2812，就是 IRC 协议的官方规则书。如果你想搞清楚某个命令具体该怎么写，最终都得回去查这些文档。

简单来说，这个项目就是让你用代码“造一部电话机”（Socket），再按照“聊天规则书”（IRC 协议）写一个自动回复的机器人，以及一个负责转接的服务器。

## Run it (on the Windows 10 VM)

```powershell
# from the folder that contains bot.py + facts.txt
python bot.py
# or override any default:
python bot.py --host fc00:1337::17 --port 6667 --name SuperBot --channel #test
```

Make sure the Ubuntu VM is already running `python3 server.py` first.

## How it maps to the marking criteria

| Brief requirement (A.2)                          | Where in code                          |
|--------------------------------------------------|----------------------------------------|
| Connect to server                                | `connect()` / IPv6 `AF_INET6` socket   |
| Maintain the connection (keep-alive)             | `PING` -> `PONG` handler; auto-reconnect loop |
| Join a channel                                   | `JOIN` after `001`                     |
| Track members (for random slap)                  | `353`/`JOIN`/`PART`/`NICK`/`QUIT` parsers |
| `!hello` greets the sender                       | `_handle_command`                      |
| `!slap [target]` (excludes bot & sender; slap sender if target absent) | `_do_slap` |
| Reply to every private message with a random fact | `_reply_private` (reads `facts.txt`)  |
| One extra feature using a new IRC command        | `!me` = **CTCP ACTION**; `!who` = **WHO** command |
| Must accept `--host/--port/--name/--channel`     | `parse_args` (defaults match lab sheet) |

## Files

- `bot.py` — the bot (no third-party dependencies).
- `facts.txt` — pool of fun-facts / nonsense lines for private-message replies.
  Add or edit lines freely; one line = one reply.

## Submission layout (per Brief, Section C.3)

The final `group_XX_source.zip` must contain a `bot/` folder (this code) and a
`server/` folder (your classmate's server). Place these two files inside `bot/`.
