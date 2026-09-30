# DI31001 Coursework — IRC Bot (client part)

Pure-standard-library Python IRC bot that connects over **IPv6** to the group's
server (running on the Ubuntu VM) and implements the "bot" requirements from
**Project Brief, Section A.2**.

## Run it (on the Windows 10 VM)

```powershell
# from the folder that contains bot.py + facts.txt
python bot.py
# or override any default:
python bot.py --host fc00:b33f::17 --port 6667 --name SuperBot --channel #test
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
