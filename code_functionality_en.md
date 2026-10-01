# DI31001 Coursework — Code Functionality Documentation

> **Purpose**: This document explains, in plain terms, what the submitted `bot` (client) and `server` programs implement, and — most importantly — **what commands a tester actually types in a terminal / IRC client to demonstrate each feature**.
>
> **Why this exists**: The header comment in `bot.py` lists the original requirement text from the marking scheme (what must be implemented) but does not say how to test it. Sections 3 and 4 below fill that gap with concrete, typed commands. Background concepts (Socket / IRC / IPv6 / client-server) are covered in `README.md`.

---

## 1. System Overview

We implemented a simplified IRC (Internet Relay Chat) system running across **two IPv6 virtual machines**:

- **Windows 10 VM**: runs `bot.py` — the "bot client" we wrote (auto-replies, executes commands).
- **Ubuntu VM**: runs `server.py` — the "central server" written by a teammate (accepts connections, relays messages).
- The two VMs talk over IPv6:
  - Windows: `fc00:1337::19`
  - Ubuntu: `fc00:1337::17` (the server binds to `::`, i.e. all addresses, on port `6667`)
- The bot on Windows actively connects to the server on Ubuntu; once connected, chat works.

**Role analogy**: the server is the "switchboard operator" that always waits for incoming calls; the bot is the "automated agent" that connects and reacts according to rules.

---

## 2. How to Run

### 2.1 Start the server (Ubuntu VM)
```bash
python3 server.py
```
The server binds `::` (all IPv6 addresses) on port `6667` by default — no arguments needed.

### 2.2 Start the bot (Windows 10 VM)
```powershell
# From the folder containing bot.py and facts.txt:
python bot.py
```
The defaults already match the lab sheet (`--host fc00:1337::17 --port 6667 --name SuperBot --channel #test`), so plain `python bot.py` works. To override:
```powershell
python bot.py --host fc00:1337::17 --port 6667 --name SuperBot --channel #test
```

### 2.3 Test client
The marking scheme repeatedly mentions **HexChat** (a GUI IRC client). In HexChat:
- Create a server with address `fc00:1337::17`, port `6667`, and tick "Use IPv6";
- Pick any nick (e.g. `Alice`);
- After connecting, type `/join #test` to enter the test channel.

(Any IRC client works; HexChat best matches the "hexchat can display it" language in the criteria.)

---

## 3. Bot functionality (Marking Items E–I) — and what you actually type

> **Key distinction**:
> - **Commands you type in the client** (high-level, e.g. `!hello`, `/join`, double-clicking a private chat) — what you do to demonstrate.
> - **IRC protocol commands the bot sends automatically** (e.g. `NICK`, `USER`, `JOIN`, `PRIVMSG`, `PING/PONG`) — handled by code, you never type these.

### Command quick-reference table

| Feature to test | What you type in HexChat | Underlying IRC command(s) |
|---|---|---|
| Make the bot connect | Nothing — `python bot.py` connects automatically | `NICK` + `USER` → registration |
| Make the bot join | You `/join #test` (bot joins on its own) | `JOIN` |
| See the bot's user list | Automatic on join; or `/names` | `353` (names), `JOIN`/`PART` |
| Private-message the bot for a random fact | Double-click `SuperBot`, send any line | `PRIVMSG` (private) |
| `!hello` | Type `!hello` in `#test` | `PRIVMSG` (bot detects `!` prefix) |
| `!slap` / `!slap Alice` | Type `!slap` or `!slap <name>` in `#test` | `PRIVMSG` |
| `!me waves` (action) | Type `!me waves` in `#test` | `PRIVMSG` + **CTCP ACTION** |
| `!who` (member count) | Type `!who` in `#test` | `PRIVMSG` + **WHO** |
| Keep-alive | Nothing — automatic every 30s | `PING` / `PONG` |

### Item E — Connect, handle errors, stay connected
- **Requirement**: The bot connects to the miniircd server, identifies itself, keeps the connection alive, and reacts to common connection errors.
- **Implementation**:
  - `connect()` opens an IPv6 (`AF_INET6`) socket and sends `NICK` + `USER` to register;
  - on server `PING` it immediately replies `PONG` (keep-alive);
  - the main loop `run()` auto-reconnects after 3s if the link drops;
  - on nickname collision (`433`) it appends `_` and retries.
- **What you type**: Nothing — it connects on launch. To demo reconnection, stop and restart `server.py` on Ubuntu and watch the bot reconnect.

### Item F — Join a channel, retain and track the user list
- **Requirement**: The bot joins a channel and keeps the member list up to date (used for random slap).
- **Implementation**: after server `001` (welcome) the bot auto-`JOIN #test`; it updates `self.users` on `353` (names), `JOIN`, `PART`, `NICK`, `QUIT`.
- **What you type**: `/join #test` in HexChat (your nick then enters the bot's list); join/leave and the list updates live.

### Item G — Auto-reply to private messages with a random fact/joke
- **Requirement**: Anyone private-messaging the bot gets a random nonsense/fun-fact line.
- **Implementation**: `_reply_private()` detects a private message (target == self) and replies via `PRIVMSG` with a random line from `facts.txt` (local file, works offline).
- **What you type**: Double-click `SuperBot` to open a private window and send any line; the bot replies with a random fact.

### Item H — Channel commands `!hello` / `!slap`
- **Requirement**: The bot reacts to `!`-prefixed commands in the channel.
- **Implementation**: `_handle_command()` handles:
  - `!hello` → "Hello <sender>! Nice to meet you."
  - `!slap [target]` → slaps a random member (excluding bot and sender); with a named target it slaps that user, or the sender if the user is absent (matches the criteria).
- **What you type** in `#test`:
  ```
  !hello
  !slap
  !slap Alice
  ```

### Item I — Extra feature (one IRC command not used previously)
- **Requirement**: Implement an extra feature using an IRC command not used earlier in the bot.
- **Implementation**: Two extras, both using new commands:
  - `!me <text>` uses **CTCP ACTION** (a sub-protocol carried inside `PRIVMSG`), e.g. `!me waves` shows "* SuperBot waves".
  - `!who` uses the **WHO** command (never used before), the server replies `352`/`315`, and the bot reports the member count.
- **What you type** in `#test`: `!me waves` or `!who`.

---

## 4. Server functionality (Marking Items J–N) — and what you actually type

The server was written by a teammate; documented here against the same criteria.

### Item J — Allow connections from multiple IRC clients
- **Requirement**: The server accepts multiple clients (e.g. HexChat), remembers login info, handles them unambiguously and efficiently.
- **Implementation**: `ClientSession` (per connection), `Channel`, and `IRCServer` using a `selectors` event loop; on `NICK`+`USER` it replies `001`–`005`/`375`/`376`.
- **What you type**: Connect two HexChat instances (different nicks) to the same server and chat — proves multi-client support.

### Item K — React to errors / unusual circumstances
- **Requirement**: Unknown commands, invalid nicks, malformed messages, and dead clients are handled without crashing or sending garbage.
- **Implementation**: unknown command → `421`; invalid/duplicate nick → `432`/`433`; malformed line → isolated to that client (only `421`, server never crashes); a client idle > 1 min gets a `PING`, then is dropped and cleaned up on timeout.
- **What you type**: Set an invalid nick in HexChat to see a clean error (no crash); disconnect a client to see the server clean it up.

### Item L — Private messaging between clients
- **Requirement**: Clients can private-message; messages reach only the intended recipient.
- **Implementation**: `_cmd_privmsg` + `_route_message`: `#`-prefixed target = channel broadcast, otherwise routed by nick; unknown recipient → `401`.
- **What you type**: Private-message between two HexChat clients; confirm only the recipient sees it.

### Item M — Join and leave channels
- **Requirement**: Clients can join/leave channels; others in the channel see it.
- **Implementation**: `_cmd_join` / `_cmd_part`: on join broadcasts `JOIN` and replies `331` (no topic) / `353` (names) / `366` (end); on part broadcasts `PART`.
- **What you type**: One client `/join #test` — others see "X joined"; `/part` likewise.

### Item N — Channel talking
- **Requirement**: Everyone in a channel sees each other's messages, and only the messages they should (no self-echo).
- **Implementation**: The channel branch of `_cmd_privmsg` calls `_broadcast_channel`, forwarding to everyone except the sender (HexChat already shows the sender's own line locally, so the server does not echo).
- **What you type**: Post in `#test`; everyone else sees it, you do not receive a duplicate.

---

## 5. End-to-end demonstration walkthrough

1. Ubuntu VM: `python3 server.py`
2. Windows VM: `python bot.py`
3. HexChat → connect to `fc00:1337::17:6667` (IPv6), nick `Alice`, `/join #test`
4. In channel: `!hello` → bot replies "Hello Alice! Nice to meet you."
5. `!slap` → bot slaps a member
6. `!slap Bob` → bot slaps Bob (or you if Bob is absent)
7. `!me waves` → shows "* SuperBot waves"
8. `!who` → bot reports "WHO returned N member record(s)..."
9. Double-click `SuperBot`, send a line → bot replies with a random fact

---