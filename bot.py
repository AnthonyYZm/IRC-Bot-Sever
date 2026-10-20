#!/usr/bin/env python3
"""IPv6 IRC bot for the DI31001 socket-programming assignment.

The required ``!hello`` and ``!slap`` commands are joined by ``!who`` and
``!notice``.  Those two extra features exercise the IRC WHO and NOTICE
commands, which are not needed by the core bot behaviour.
"""

from __future__ import annotations

import argparse
from collections import deque
import os
import random
import socket
import sys
import time

CRLF = b"\r\n"
RECV_CHUNK = 4096
CTCP = "\x01"  # CTCP framing character
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 1.0
CONNECT_RETRY_DELAY = 5.0
RECONNECT_DELAY = 3.0
IRC_CASE_TRANSLATION = str.maketrans({"[": "{", "]": "}", "\\": "|", "^": "~"})

# Defaults match the two-VM lab sheet.  IRC_HOST and --host remain available
# for loopback tests or a different server.
DEFAULT_HOST = os.environ.get("IRC_HOST", "fc00:1337::17")
DEFAULT_PORT = 6667              # server listens on the usual IRC port 6667
DEFAULT_NAME = "SuperBot"        # brief's example nickname
DEFAULT_CHANNEL = "#test"        # lab sheet / slides test channel

FACTS_FILE = "facts.txt"


def irc_casefold(value: str) -> str:
    """Apply the RFC 2812 casemapping used for nick and channel matching."""
    return value.lower().translate(IRC_CASE_TRANSLATION)


def parse_irc_line(line: str) -> tuple[str | None, str, list[str]]:
    """Split one IRC message into (prefix, command, params).

    The trailing parameter (after ' :') may contain spaces and is always
    returned as the last element of params.
    """
    prefix = None
    if line.startswith(":"):
        try:
            prefix, line = line[1:].split(" ", 1)
        except ValueError as exc:
            raise ValueError("prefix without command") from exc
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


def format_irc(command: str, *params: str) -> str:
    """Build one IRC command without performing network I/O."""
    chunks = [command]
    for index, param in enumerate(params):
        is_last = index == len(params) - 1
        if is_last and (not param or " " in param or param.startswith(":")):
            # The first colon is protocol framing; any colon already present in
            # the value is data and must be preserved.
            chunks.append(":" + param)
        else:
            chunks.append(param)
    return " ".join(chunks)


def make_privmsg(target: str, text: str) -> str:
    """Build a PRIVMSG while keeping response creation outside socket code."""
    return format_irc("PRIVMSG", target, text)


def nick_from_prefix(prefix: str | None) -> str:
    """Extract the nickname from a 'nick!user@host' prefix."""
    if not prefix:
        return ""
    return prefix.split("!")[0].split("@")[0]


class IRCBot:
    """Maintain one IRC connection and the member view of one channel."""

    def __init__(self, host: str, port: int, name: str, channel: str, facts: list[str]) -> None:
        self.host = host
        self.port = port
        self.nick = name               # current nick (may change on collision)
        self.channel = channel
        self.facts = facts

        self.sock: socket.socket | None = None
        self._recv_buf = b""
        self.users: dict[str, str] = {}  # casefold(nick) -> display nick; members excluding self
        self._names_pending = False
        self._names_suppressed = False
        self._names_snapshot: dict[str, str] = {}
        self._names_changes: list[tuple[str, str, str]] = []
        self._who_counts: deque[int] = deque()
        self.connected = False
        self.running = True

    # ----------------------------- networking ----------------------------- #
    def connect(self) -> None:
        """Open the IPv6 TCP connection and register (NICK + USER)."""
        self.disconnect()
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        print(f"[connect] -> [{self.host}]:{self.port} (IPv6)")
        try:
            sock.connect((self.host, self.port))
        except OSError:
            sock.close()
            raise
        sock.settimeout(READ_TIMEOUT)
        self.sock = sock
        self.connected = True
        self._recv_buf = b""
        self.users.clear()
        self._names_pending = False
        self._names_suppressed = False
        self._names_snapshot.clear()
        self._names_changes.clear()
        self._who_counts.clear()
        self._register()

    def _register(self) -> None:
        self._send(format_irc("NICK", self.nick))
        self._send(format_irc("USER", self.nick, "0", "*", f"{self.nick} coursework bot"))

    def _send(self, line: str) -> None:
        if self.sock is None:
            return
        try:
            self.sock.sendall((line + "\r\n").encode("utf-8"))
            print(">>", line)
        except OSError as exc:
            print("[send error]", exc)
            self.disconnect()

    def disconnect(self) -> None:
        """Close the current socket; calling this more than once is safe."""
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
                self.disconnect()
                print(f"[connect failed] {exc} -- retrying in {CONNECT_RETRY_DELAY:g}s")
                time.sleep(CONNECT_RETRY_DELAY)
                continue
            try:
                self._read_loop()
            except OSError as exc:
                if self.running:
                    print(f"[connection lost] {exc}")
            self.disconnect()
            if self.running:
                print(f"[reconnect] waiting {RECONNECT_DELAY:g}s before reconnecting...")
                time.sleep(RECONNECT_DELAY)

    def _read_loop(self) -> None:
        while self.connected and self.running:
            sock = self.sock
            if sock is None:
                break
            try:
                data = sock.recv(RECV_CHUNK)
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
        try:
            prefix, cmd, params = parse_irc_line(line)
        except ValueError as exc:
            print(f"[protocol warning] ignoring malformed line: {exc}")
            return

        if cmd == "PING":
            self._handle_ping(params)
        elif cmd.isdigit():
            self._handle_numeric(cmd, params)
        elif cmd == "JOIN":
            self._handle_join(prefix, params)
        elif cmd == "PART":
            self._handle_part(prefix, params)
        elif cmd == "KICK":
            self._handle_kick(params)
        elif cmd == "QUIT":
            self._handle_quit(prefix)
        elif cmd == "NICK":
            self._handle_nick(prefix, params)
        elif cmd == "PRIVMSG":
            self._handle_privmsg(prefix, params)

    def _handle_ping(self, params: list[str]) -> None:
        """Answer the server's liveness probe without changing bot state."""
        token = params[-1] if params else ""
        self._send(format_irc("PONG", token))

    def _handle_numeric(self, command: str, params: list[str]) -> None:
        """Dispatch server numerics that affect registration or bot state."""
        if command == "001":
            print(f"[registered] joining {self.channel}")
            self._send(format_irc("JOIN", self.channel))
        elif command == "433":
            self.nick += "_"
            print(f"[433] nickname taken, retrying as {self.nick}")
            self._send(format_irc("NICK", self.nick))
        elif command == "353":
            self._handle_names_reply(params)
        elif command == "366":
            self._finish_names_reply(params)
        elif command == "352":
            self._record_who_reply(params)
        elif command == "315":
            self._report_who(params)

    def _begin_names_reply(self) -> None:
        self._names_pending = True
        self._names_suppressed = False
        self._names_snapshot.clear()
        self._names_changes.clear()

    def _forget_channel(self) -> None:
        """Discard an in-flight NAMES batch after the bot leaves its channel."""
        self.users.clear()
        self._names_pending = False
        self._names_suppressed = True
        self._names_snapshot.clear()
        self._names_changes.clear()

    def _handle_names_reply(self, params: list[str]) -> None:
        """Accumulate one RPL_NAMREPLY line for the configured channel."""
        if self._names_suppressed:
            return
        if (
            len(params) < 4
            or params[1] != "="
            or irc_casefold(params[2]) != irc_casefold(self.channel)
        ):
            return
        if not self._names_pending:
            self._begin_names_reply()
        self._update_users_from_names(params[3])

    def _finish_names_reply(self, params: list[str]) -> None:
        if len(params) < 2 or irc_casefold(params[1]) != irc_casefold(self.channel):
            return
        if self._names_suppressed:
            self._names_suppressed = False
            return
        names = self._names_snapshot.copy() if self._names_pending else {}
        for change, old, new in self._names_changes:
            old_key = irc_casefold(old)
            if change == "add":
                names[irc_casefold(new)] = new
            elif change == "remove":
                names.pop(old_key, None)
            elif change == "rename" and old_key in names:
                names.pop(old_key)
                names[irc_casefold(new)] = new
        names.pop(irc_casefold(self.nick), None)
        self.users = names
        self._names_pending = False
        self._names_snapshot.clear()
        self._names_changes.clear()
        print(f"[names] member list complete ({len(self.users)} users)")

    def _record_names_change(self, change: str, old: str, new: str = "") -> None:
        if self._names_pending:
            self._names_changes.append((change, old, new))

    def _handle_join(self, prefix: str | None, params: list[str]) -> None:
        who = nick_from_prefix(prefix)
        channel = params[0] if params else ""
        if who and irc_casefold(channel) == irc_casefold(self.channel):
            if irc_casefold(who) == irc_casefold(self.nick):
                self._begin_names_reply()
            else:
                self._add_user(who)
                self._record_names_change("add", "", who)

    def _handle_part(self, prefix: str | None, params: list[str]) -> None:
        who = nick_from_prefix(prefix)
        channel = params[0] if params else ""
        if who and irc_casefold(channel) == irc_casefold(self.channel):
            if irc_casefold(who) == irc_casefold(self.nick):
                self._forget_channel()
            else:
                self._remove_user(who)
                self._record_names_change("remove", who)

    def _handle_kick(self, params: list[str]) -> None:
        channel = params[0] if params else ""
        kicked_nick = params[1] if len(params) >= 2 else ""
        if irc_casefold(channel) != irc_casefold(self.channel):
            return
        if irc_casefold(kicked_nick) == irc_casefold(self.nick):
            self._forget_channel()
        else:
            self._remove_user(kicked_nick)
            self._record_names_change("remove", kicked_nick)

    def _handle_quit(self, prefix: str | None) -> None:
        who = nick_from_prefix(prefix)
        if who:
            self._remove_user(who)
            self._record_names_change("remove", who)

    def _handle_nick(self, prefix: str | None, params: list[str]) -> None:
        old = nick_from_prefix(prefix)
        new = params[0] if params else ""
        if old and new:
            self._record_names_change("rename", old, new)
        if irc_casefold(old) == irc_casefold(self.nick):
            self.nick = new
        elif self._irc_lower(old) in self.users:
            self._remove_user(old)
            if new:
                self._add_user(new)

    # --------------------------- user tracking ---------------------------- #
    @staticmethod
    def _irc_lower(name: str) -> str:
        return irc_casefold(name)

    def _add_user(self, display: str) -> None:
        if display and self._irc_lower(display) != self._irc_lower(self.nick):
            self.users[self._irc_lower(display)] = display

    def _remove_user(self, display: str) -> None:
        self.users.pop(self._irc_lower(display), None)

    def _random_other(self, exclude: str) -> str | None:
        """Random member that is neither the bot nor `exclude` (case-insensitive)."""
        excl = {self._irc_lower(self.nick), self._irc_lower(exclude)}
        pool = [d for k, d in self.users.items() if k not in excl]
        return random.choice(pool) if pool else None

    def _update_users_from_names(self, names_str: str) -> None:
        for n in names_str.split():
            n = n.strip().lstrip("@+~&%")  # strip channel-status prefixes
            if n:
                self._names_snapshot[irc_casefold(n)] = n

    # --------------------------- PRIVMSG logic ---------------------------- #
    def _handle_privmsg(self, prefix: str | None, params: list[str]) -> None:
        if len(params) < 2:
            return
        target = params[0]
        text = params[1]
        sender = nick_from_prefix(prefix)

        # Private message -> random fun-fact (brief: reply to each PM).
        if irc_casefold(target) == irc_casefold(self.nick):
            self._reply_private(sender)
            return

        # Channel message -> only react to '!' commands.
        if irc_casefold(target) == irc_casefold(self.channel) and text.startswith("!"):
            self._handle_command(sender, text)

    def _reply_private(self, sender: str) -> None:
        if not sender:
            return
        fact = random.choice(self.facts)
        self._send(make_privmsg(sender, fact))

    def _handle_command(self, sender: str, text: str) -> None:
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "!hello":
            self._send(make_privmsg(self.channel, f"Hello {sender}! Nice to meet you."))

        elif cmd == "!slap":
            self._do_slap(sender, arg)

        elif cmd == "!me":
            # Extra feature (Item I): CTCP ACTION emote. This exercises the
            # CTCP sub-protocol carried inside PRIVMSG, which the bot does not
            # use anywhere else.
            if arg:
                self._send(make_privmsg(self.channel, f"{CTCP}ACTION {arg}{CTCP}"))

        elif cmd == "!who":
            # Extra feature (Item I): use the WHO command, which is not used
            # anywhere else in the bot. The server replies with 352/315.
            self._send(make_privmsg(self.channel, "Counting members via WHO..."))
            self._who_counts.append(0)
            self._send(format_irc("WHO", self.channel))

        elif cmd == "!notice":
            # Extra feature: NOTICE is a distinct IRC command and intentionally
            # does not trigger automatic replies at the protocol level.
            notice_parts = arg.split(None, 1)
            if len(notice_parts) != 2:
                self._send(make_privmsg(self.channel, "Usage: !notice <nick> <message>"))
                return
            target, message = notice_parts
            self._send(format_irc("NOTICE", target, message))

    def _do_slap(self, sender: str, target: str) -> None:
        # The H4 rubric requires the victim to be neither the bot nor sender.
        # Nickname comparisons therefore use the RFC 2812 casemapping.
        bot_key = self._irc_lower(self.nick)
        sender_key = self._irc_lower(sender)
        victim: str | None

        if target:
            tgt_key = self._irc_lower(target)
            if tgt_key == bot_key:
                self._send(make_privmsg(self.channel, f"Hands off the bot, {sender}!"))
                return
            if tgt_key == sender_key:
                self._send(make_privmsg(self.channel, f"I'm not slapping you, {sender}."))
                return
            if tgt_key in self.users:
                victim = self.users[tgt_key]
            else:
                self._send(make_privmsg(self.channel, f"{target} is not in {self.channel}."))
                return
        else:
            victim = self._random_other(sender)
            if victim is None:
                self._send(make_privmsg(self.channel, f"Nobody else to slap here, {sender}!"))
                return

        assert victim is not None
        self._send(make_privmsg(
            self.channel,
            f"{CTCP}ACTION slaps {victim} around a bit with a large trout{CTCP}",
        ))

    def _record_who_reply(self, params: list[str]) -> None:
        if self._who_counts and len(params) >= 2:
            if irc_casefold(params[1]) == irc_casefold(self.channel):
                self._who_counts[0] += 1

    def _report_who(self, params: list[str]) -> None:
        if not self._who_counts or len(params) < 2:
            return
        if irc_casefold(params[1]) != irc_casefold(self.channel):
            return
        count = self._who_counts.popleft()
        self._send(make_privmsg(
            self.channel,
            f"WHO returned {count} member record(s) for {self.channel}.",
        ))


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
def load_facts(path: str) -> list[str]:
    """Load non-empty fact lines, with a safe fallback if the file is unusable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            facts = [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        facts = []
    if not facts:
        facts = ["I am a very small bot and I forgot my facts file."]
    return facts


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the four optional command-line values required by the brief."""
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
    """Load configuration and run the reconnecting bot until interrupted."""
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
