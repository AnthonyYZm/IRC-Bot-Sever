#!/usr/bin/env python3
"""
原文功能要求：
Connecting to a server，
Maintaining the connection to the server，
Joining a channel
Responding to channel messages starting with a '!' character and more specifically:
"!hello" greeting the user sending it
"!slap" slapping a random user in the channel with a trout excluding the bot and the user sending it. The command should also support an optional parameter to slap a specific user and slapping the user sending it if that user is not in the channel.
Responding to each private message by a random sentence (nonsense or fun-fact)
Provide one extra feature of your choice using any IRC command not used previously。对于最后一个要求，我们采用WHO（bot 全程没用过）+ CTCP ACTION（PRIVMSG 里套的特殊子协议，基本功能也没用过）。指令是

To make it easier to be used and tested, your submitted program must be able to take the following optional command line parameters:

"--host 2001:db8::1337" indicating the server the bot should connect to
"--port 6666" indicating the port the bot should connect to
"--name SuperBot" indicating the nickname the bot should use
"--channel #hello" indicating the channel the bot should join and monitor for commands
Design notes:
  * Pure Python standard library only (socket, argparse, random, re, time).
    Dependency-free so it runs as-is on the Windows VM.
  * Plain IPv6 TCP, no TLS (TLS is not required by the brief).

Usage (defaults already match the lab sheet, so 'python bot.py' just works):
    python bot.py
    python bot.py --host fc00:1337::17 --port 6667 --name SuperBot --channel #test
"""

from __future__ import annotations

import argparse
import os
import random
import socket
import sys
import time

CRLF = b"\r\n"
RECV_CHUNK = 4096
CTCP = "\x01"  # CTCP framing character

# Defaults that match the lab sheet / brief examples, so the bot "just works"
# on the provided VMs. All four can be overridden on the CLI, which the brief
# requires (omitting them costs up to 5 marks).
#
# 地址依据（配置 VM 的权威文档 Lab Sheet 2 + correctAddress 更正文档）：
#   Windows VM IPv6 = fc00:1337::19/96   (Lab Sheet 2 B.2)  <- bot 跑在这台（客户端）
#   Ubuntu  VM IPv6 = fc00:1337::17/96   (correctAddress 文档)  <- server 跑在这台，bot 连它
# ⚠️ Lab Sheet 2 B.3 原文把 Ubuntu 写成 fc00:b33f::17，但那是错的前缀：
#   它和 Windows 的 1337 前缀不同 → 两 VM 不在同一子网 → IPv6 互 ping 不通
#   （这正是 Lab Sheet 2 B.4 让你"改 Ubuntu 配置"的原因）。
#   correctAddress 文档已更正：Ubuntu 也用 1337 前缀，两 VM 同子网可直接通信。
#   所以 bot 连接目标 = Ubuntu = fc00:1337::17。若你手抖把 Ubuntu 配回了 b33f，启动加 --host fc00:b33f::17。
DEFAULT_HOST = "fc00:1337::17"   # Ubuntu VM IPv6 (correctAddress 更正; Lab Sheet 2 B.3 原 b33f 为错)
DEFAULT_PORT = 6667              # server listens on the usual IRC port 6667
DEFAULT_NAME = "SuperBot"        # brief's example nickname
DEFAULT_CHANNEL = "#test"        # lab sheet / slides test channel

FACTS_FILE = "facts.txt"


# --------------------------------------------------------------------------- #
# Small protocol helpers (no socket dependency, easy to reason about)
# --------------------------------------------------------------------------- #
def parse_irc_line(line: str):
    """Split one IRC message into (prefix, command, params).

    The trailing parameter (after ' :') may contain spaces and is always
    returned as the last element of params.
    """
    prefix = None
    if line.startswith(":"):
        prefix, line = line[1:].split(" ", 1)
    trailing = None
    if " :" in line:
        line, trailing = line.split(" :", 1)
    parts = line.split()
    if not parts:
        return prefix, "", []
    command = parts[0].upper()
    params = parts[1:]
    if trailing is not None:
        params.append(trailing)
    return prefix, command, params


def nick_from_prefix(prefix: str | None) -> str:
    """Extract the nickname from a 'nick!user@host' prefix."""
    if not prefix:
        return ""
    return prefix.split("!")[0].split("@")[0]


# --------------------------------------------------------------------------- #
# The bot
# --------------------------------------------------------------------------- #
class IRCBot:
    def __init__(self, host: str, port: int, name: str, channel: str, facts: list[str]) -> None:
        self.host = host
        self.port = port
        self.base_name = name          # desired nick
        self.nick = name               # current nick (may change on collision)
        self.channel = channel
        self.facts = facts

        self.sock: socket.socket | None = None
        self._recv_buf = b""
        self.users: set[str] = set()   # channel members, excluding self
        self._who_replies: list[list[str]] = []  # accumulated WHO 352 records
        self.connected = False
        self.running = True

    # ----------------------------- networking ----------------------------- #
    def connect(self) -> None:
        """Open the IPv6 TCP connection and register (NICK + USER)."""
        self.sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self.sock.settimeout(1.0)
        print(f"[connect] -> [{self.host}]:{self.port} (IPv6)")
        self.sock.connect((self.host, self.port))
        self.connected = True
        self._recv_buf = b""
        self.users.clear()
        self._register()

    def _register(self) -> None:
        self._send(f"NICK {self.nick}")
        self._send(f"USER {self.nick} 0 * :{self.nick} coursework bot")

    def _send(self, line: str) -> None:
        if self.sock is None:
            return
        try:
            self.sock.sendall((line + "\r\n").encode("utf-8"))
            print(">>", line)
        except OSError as exc:
            print("[send error]", exc)
            self.connected = False

    def disconnect(self) -> None:
        self.connected = False
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def run(self) -> None:
        """Main loop with auto-reconnect (brief: maintain the connection)."""
        while self.running:
            try:
                self.connect()
            except OSError as exc:
                print(f"[connect failed] {exc} -- retrying in 5s")
                time.sleep(5)
                continue
            try:
                self._read_loop()
            except OSError as exc:
                print(f"[connection lost] {exc}")
            self.disconnect()
            if self.running:
                print("[reconnect] waiting 3s before reconnecting...")
                time.sleep(3)

    def _read_loop(self) -> None:
        while self.connected and self.running:
            try:
                data = self.sock.recv(RECV_CHUNK)
            except socket.timeout:
                continue
            if not data:
                self.connected = False
                break
            self._recv_buf += data
            while CRLF in self._recv_buf:
                raw, self._recv_buf = self._recv_buf.split(CRLF, 1)
                self._handle(raw.decode("utf-8", "replace"))

    # ----------------------------- protocol ------------------------------- #
    def _handle(self, line: str) -> None:
        print("<<", line)
        prefix, cmd, params = parse_irc_line(line)

        # Server liveness ping -> must pong back (brief: maintain connection).
        if cmd == "PING":
            token = params[-1] if params else ""
            self._send(f"PONG {token}")
            return

        if cmd == "001":  # RPL_WELCOME -> registered, join the channel.
            print(f"[registered] joining {self.channel}")
            self._send(f"JOIN {self.channel}")
            return

        if cmd == "433":  # ERR_NICKNAMEINUSE -> tweak and retry.
            self.nick = self.nick + "_"
            print(f"[433] nickname taken, retrying as {self.nick}")
            self._send(f"NICK {self.nick}")
            return

        if cmd == "353":  # RPL_NAMREPLY -> populate/refresh member list.
            # params: [target, '=', channel, "nick1 nick2 ..."]
            if len(params) >= 4 and params[1] == "=":
                self._update_users_from_names(params[3])
            return

        if cmd == "JOIN":
            who = nick_from_prefix(prefix)
            if who and who != self.nick:
                self.users.add(who)
            return

        if cmd == "PART":
            who = nick_from_prefix(prefix)
            if who:
                self.users.discard(who)
            return

        if cmd == "QUIT":
            who = nick_from_prefix(prefix)
            if who:
                self.users.discard(who)
            return

        if cmd == "NICK":  # someone (or us) changed nickname.
            old = nick_from_prefix(prefix)
            new = params[0] if params else ""
            if old == self.nick:
                self.nick = new
            elif old in self.users:
                self.users.discard(old)
                if new:
                    self.users.add(new)
            return

        if cmd == "PRIVMSG":
            self._handle_privmsg(prefix, params)
            return

        if cmd == "352":  # RPL_WHOREPLY -> accumulate (extra feature !who).
            self._who_replies.append(params)
            return

        if cmd == "315":  # RPL_ENDOFWHO -> report the collected result.
            self._report_who()
            return

    def _update_users_from_names(self, names_str: str) -> None:
        for n in names_str.split():
            n = n.strip().lstrip("@+~&%")  # strip channel-status prefixes
            if n and n != self.nick:
                self.users.add(n)

    # --------------------------- PRIVMSG logic ---------------------------- #
    def _handle_privmsg(self, prefix: str, params: list[str]) -> None:
        if len(params) < 2:
            return
        target = params[0]
        text = params[1]
        sender = nick_from_prefix(prefix)

        # Private message -> random fun-fact (brief: reply to each PM).
        if target == self.nick:
            self._reply_private(sender)
            return

        # Channel message -> only react to '!' commands.
        if target == self.channel and text.startswith("!"):
            self._handle_command(sender, text)

    def _reply_private(self, sender: str) -> None:
        fact = random.choice(self.facts)
        self._send(f"PRIVMSG {sender} :{fact}")
# 对于!hello，我们采用PRIVMSG。

    def _handle_command(self, sender: str, text: str) -> None:
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "!hello":
            self._send(f"PRIVMSG {self.channel} :Hello {sender}! Nice to meet you.")

        elif cmd == "!slap":
            self._do_slap(sender, arg)

        elif cmd == "!me":
            # Extra feature (Item I): CTCP ACTION emote. This exercises the
            # CTCP sub-protocol carried inside PRIVMSG, which the bot does not
            # use anywhere else.
            if arg:
                self._send(f"PRIVMSG {self.channel} :{CTCP}ACTION {arg}{CTCP}")

        elif cmd == "!who":
            # Extra feature (Item I): use the WHO command, which is not used
            # anywhere else in the bot. The server replies with 352/315.
            self._send(f"PRIVMSG {self.channel} :Counting members via WHO...")
            self._who_replies = []
            self._send(f"WHO {self.channel}")

    def _do_slap(self, sender: str, target: str) -> None:
        if target:
            # Named target: slap them, or slap the sender if they aren't present
            # (per the brief: slap the sender when the named user is not around).
            victim = target if target in self.users else sender
        else:
            # Random member, excluding the sender and the bot itself.
            candidates = [u for u in self.users if u != sender]
            victim = random.choice(candidates) if candidates else sender
        self._send(
            f"PRIVMSG {self.channel} :{CTCP}ACTION slaps {victim} around a bit "
            f"with a large trout{CTCP}"
        )

    def _report_who(self) -> None:
        n = len(self._who_replies)
        self._send(
            f"PRIVMSG {self.channel} :WHO returned {n} member record(s) for {self.channel}."
        )
        self._who_replies = []


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
def load_facts(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            facts = [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        facts = []
    if not facts:
        facts = ["I am a very small bot and I forgot my facts file."]
    return facts


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DI31001 coursework IRC bot (client).")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"IPv6 address of the server (default: {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"TCP port (default: {DEFAULT_PORT})")
    p.add_argument("--name", default=DEFAULT_NAME,
                   help=f"bot nickname (default: {DEFAULT_NAME})")
    p.add_argument("--channel", default=DEFAULT_CHANNEL,
                   help=f"channel to join (default: {DEFAULT_CHANNEL})")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    # Always locate facts.txt next to this script, so the bot works no matter
    # which directory it is launched from.
    here = os.path.dirname(os.path.abspath(__file__))
    facts_path = os.path.join(here, FACTS_FILE)
    facts = load_facts(facts_path)
    print(f"[facts] loaded {len(facts)} lines from {facts_path}")
    bot = IRCBot(args.host, args.port, args.name, args.channel, facts)
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\n[bye] shutting down")
        bot.running = False
        bot.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
