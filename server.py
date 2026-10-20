#!/usr/bin/env python3
"""Small IPv6 IRC server for the DI31001 socket-programming assignment."""

from __future__ import annotations

import argparse
import logging
import re
import selectors
import socket
import time
import uuid
from dataclasses import dataclass, field


CRLF = b"\r\n"
MAX_IRC_LINE_BYTES = 512
MAX_RECV_BUFFER = 8192
MAX_SEND_BUFFER = 1024 * 1024
PING_AFTER = 30.0
DROP_AFTER_PING = 30.0
SERVER_NAME = "irc.assignment.local"
SERVER_VERSION = "course-irc-1.0"
IRC_CASE_TRANSLATION = str.maketrans({"[": "{", "]": "}", "\\": "|", "^": "~"})

NICK_RE = re.compile(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}-]{0,29}$")


def irc_casefold(value: str) -> str:
    """Apply IRC's case-insensitive nickname and channel mapping."""
    return value.lower().translate(IRC_CASE_TRANSLATION)


def valid_channel_name(name: str) -> bool:
    """Return whether a channel name is safe for this coursework subset."""
    if not name.startswith("#") or len(name) < 2 or len(name) > 50:
        return False
    return not any(ch in name for ch in (" ", ",", "\x00", "\x07", "\r", "\n", ":"))


@dataclass(frozen=True)
class IRCMessage:
    """Parsed IRC message independent of socket handling."""

    prefix: str | None
    command: str
    params: tuple[str, ...] = ()


def parse_irc_line(line: str) -> IRCMessage:
    """Parse one CRLF-free IRC line into its prefix, command, and parameters."""
    if any(control in line for control in ("\x00", "\r", "\n")):
        raise ValueError("IRC line contains a forbidden control character")
    if not line:
        raise ValueError("empty IRC line")

    prefix: str | None = None
    rest = line
    if rest.startswith(":"):
        try:
            prefix, rest = rest[1:].split(" ", 1)
        except ValueError as exc:
            raise ValueError("prefix without command") from exc
        rest = rest.lstrip()

    trailing: str | None = None
    if " :" in rest:
        rest, trailing = rest.split(" :", 1)

    pieces = rest.split()
    if not pieces:
        raise ValueError("missing command")

    command = pieces[0].upper()
    params = pieces[1:]
    if trailing is not None:
        params.append(trailing)

    return IRCMessage(prefix=prefix, command=command, params=tuple(params))


def format_irc(prefix: str | None, command: str, *params: str) -> str:
    """Format one IRC line without performing network I/O."""
    chunks: list[str] = []
    if prefix:
        chunks.append(f":{prefix}")
    chunks.append(command)

    for i, param in enumerate(params):
        is_last = i == len(params) - 1
        if is_last and (param == "" or " " in param or param.startswith(":")):
            # One colon marks the trailing field.  A leading colon already in
            # the parameter is message data and must not be stripped.
            chunks.append(":" + param)
        else:
            chunks.append(param)
    return " ".join(chunks)


@dataclass(eq=False)
class ClientSession:
    """Per-socket identity, membership, buffering, and liveness state."""

    sock: socket.socket
    addr: tuple[str, int, int, int]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    nickname: str | None = None
    username: str | None = None
    realname: str | None = None
    registered: bool = False
    recv_buffer: bytearray = field(default_factory=bytearray)
    send_buffer: bytearray = field(default_factory=bytearray)
    channels: set[str] = field(default_factory=set)
    last_activity: float = field(default_factory=time.monotonic)
    ping_token: str | None = None
    ping_sent_at: float | None = None
    closed: bool = False

    @property
    def host(self) -> str:
        """Return the peer's numeric IPv6 address."""
        return str(self.addr[0]) if self.addr else "unknown"

    def prefix(self) -> str:
        """Return the nick!user@host prefix used in relayed messages."""
        nick = self.nickname or "*"
        user = self.username or "unknown"
        return f"{nick}!{user}@{self.host}"


@dataclass
class Channel:
    """Canonical channel name and its connected member sessions."""

    name: str
    members: set[ClientSession] = field(default_factory=set)


class IRCServer:
    """Serve multiple IPv6 IRC clients with one non-blocking selector loop."""

    def __init__(self, host: str = "::", port: int = 6667) -> None:
        self.host = host
        self.port = port
        self.selector = selectors.DefaultSelector()
        self.listener: socket.socket | None = None
        self.sessions: dict[socket.socket, ClientSession] = {}
        self.nick_index: dict[str, ClientSession] = {}
        self.channels: dict[str, Channel] = {}
        self.running = False

    def start(self) -> None:
        """Bind the IPv6 listener and run until stopped or interrupted."""
        listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except OSError as exc:
            logging.debug("IPV6_V6ONLY is unavailable: %s", exc)
        listener.bind((self.host, self.port))
        listener.listen()
        listener.setblocking(False)

        self.listener = listener
        self.selector.register(listener, selectors.EVENT_READ, data=None)
        self.running = True
        logging.info("Listening on [%s]:%d (IPv6)", self.host, self.port)

        try:
            while self.running:
                for key, mask in self.selector.select(timeout=1.0):
                    if key.data is None:
                        self._accept_client()
                    else:
                        session: ClientSession = key.data
                        if mask & selectors.EVENT_READ:
                            self._read_client(session)
                        if not session.closed and mask & selectors.EVENT_WRITE:
                            self._write_client(session)
                self._check_liveness()
        except KeyboardInterrupt:
            logging.info("Stopping server...")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Close all sessions, the listener, and the selector."""
        self.running = False
        for session in list(self.sessions.values()):
            self._disconnect(session, "Server shutting down", announce=False)
        if self.listener is not None:
            try:
                self.selector.unregister(self.listener)
            except KeyError:
                logging.debug("listener was already unregistered")
            try:
                self.listener.close()
            except OSError as exc:
                logging.debug("listener close failed: %s", exc)
            self.listener = None
        self.selector.close()

    def _accept_client(self) -> None:
        assert self.listener is not None
        try:
            sock, addr = self.listener.accept()
        except OSError as exc:
            logging.warning("accept() failed: %s", exc)
            return

        sock.setblocking(False)
        session = ClientSession(sock=sock, addr=addr)
        self.sessions[sock] = session
        self.selector.register(sock, selectors.EVENT_READ, data=session)
        logging.info("[%s] connected from %s", session.id, addr)

    def _read_client(self, session: ClientSession) -> None:
        try:
            chunk = session.sock.recv(4096)
        except BlockingIOError:
            return
        except (ConnectionResetError, OSError) as exc:
            logging.info("[%s] read error: %s", session.id, exc)
            self._disconnect(session, "Connection reset")
            return

        if not chunk:
            self._disconnect(session, "Connection closed")
            return

        session.recv_buffer.extend(chunk)
        if len(session.recv_buffer) > MAX_RECV_BUFFER and CRLF not in session.recv_buffer:
            self._queue_line(session, format_irc(None, "ERROR", "Input buffer exceeded"))
            self._flush_best_effort(session)
            self._disconnect(session, "Input buffer exceeded")
            return

        while True:
            pos = session.recv_buffer.find(CRLF)
            if pos < 0:
                break
            raw = bytes(session.recv_buffer[:pos])
            del session.recv_buffer[: pos + len(CRLF)]

            if len(raw) + 2 > MAX_IRC_LINE_BYTES:
                self._queue_line(session, format_irc(None, "ERROR", "IRC line too long"))
                continue

            try:
                line = raw.decode("utf-8", errors="replace")
                message = parse_irc_line(line)
            except ValueError:
                self._numeric(session, "421", "*", "Malformed command")
                continue

            session.last_activity = time.monotonic()
            session.ping_token = None
            session.ping_sent_at = None
            logging.debug("[%s] <- %s", session.id, line)
            self._dispatch(session, message)
            if session.closed:
                break

    def _write_client(self, session: ClientSession) -> None:
        if not session.send_buffer:
            self._set_interest(session, want_write=False)
            return
        try:
            sent = session.sock.send(session.send_buffer)
        except BlockingIOError:
            return
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            logging.info("[%s] write error: %s", session.id, exc)
            self._disconnect(session, "Write failed")
            return

        del session.send_buffer[:sent]
        if not session.send_buffer:
            self._set_interest(session, want_write=False)

    def _set_interest(self, session: ClientSession, want_write: bool) -> None:
        if session.closed:
            return
        events = selectors.EVENT_READ | (selectors.EVENT_WRITE if want_write else 0)
        try:
            self.selector.modify(session.sock, events, data=session)
        except (KeyError, OSError, ValueError) as exc:
            logging.warning("[%s] selector update failed: %s", session.id, exc)
            self._disconnect(session, "Selector failure")

    def _queue_line(self, session: ClientSession, line: str) -> None:
        if session.closed:
            return
        payload = line.encode("utf-8") + CRLF
        if len(payload) > MAX_IRC_LINE_BYTES:
            logging.warning("Refusing oversized server-generated IRC line (%d bytes)", len(payload))
            return
        session.send_buffer.extend(payload)
        if len(session.send_buffer) > MAX_SEND_BUFFER:
            self._disconnect(session, "Client is not reading data")
            return
        logging.debug("[%s] -> %s", session.id, line)
        self._set_interest(session, want_write=True)

    def _flush_best_effort(self, session: ClientSession) -> None:
        if session.closed or not session.send_buffer:
            return
        try:
            session.sock.send(session.send_buffer)
        except OSError as exc:
            logging.debug("[%s] final send failed: %s", session.id, exc)

    def _target(self, session: ClientSession) -> str:
        return session.nickname or "*"

    def _numeric(self, session: ClientSession, code: str, *params: str) -> None:
        self._queue_line(session, format_irc(SERVER_NAME, code, self._target(session), *params))

    @staticmethod
    def _bounded_line(prefix: str, command: str, *params: str) -> str:
        """Trim a trailing text field so a prefixed IRC line can be delivered."""
        fixed_params, text = params[:-1], params[-1]
        empty_line = format_irc(prefix, command, *fixed_params, "")
        text_budget = max(
            0,
            MAX_IRC_LINE_BYTES - len(CRLF) - len(empty_line.encode("utf-8")),
        )

        encoded = text.encode("utf-8")
        if len(encoded) > text_budget:
            text = encoded[:text_budget].decode("utf-8", errors="ignore")
        return format_irc(prefix, command, *fixed_params, text)

    @classmethod
    def _forwarded_message(
        cls,
        sender: ClientSession,
        command: str,
        target: str,
        text: str,
    ) -> str:
        """Build a relayed PRIVMSG or NOTICE within the IRC line limit."""
        return cls._bounded_line(sender.prefix(), command, target, text)

    def _require_registered(self, session: ClientSession) -> bool:
        if session.registered:
            return True
        self._numeric(session, "451", "You have not registered")
        return False

    def _try_finish_registration(self, session: ClientSession) -> None:
        if session.registered or not session.nickname or not session.username:
            return

        session.registered = True
        self._numeric(session, "001", f"Welcome to the IRC server {session.prefix()}")
        self._numeric(
            session,
            "002",
            f"Your host is {SERVER_NAME}, running version {SERVER_VERSION}",
        )
        self._numeric(
            session,
            "003",
            "This server was created for the socket programming assignment",
        )
        self._numeric(session, "004", SERVER_NAME, SERVER_VERSION, "i", "nt")
        self._numeric(session, "375", f"- {SERVER_NAME} Message of the Day -")
        self._numeric(session, "372", "- Minimal coursework IRC server")
        self._numeric(session, "376", "End of MOTD command")
        logging.info("[%s] registered as %s", session.id, session.nickname)

    def _shared_peers(self, session: ClientSession) -> set[ClientSession]:
        peers: set[ClientSession] = set()
        for key in session.channels:
            channel = self.channels.get(key)
            if channel:
                peers.update(channel.members)
        peers.discard(session)
        return peers

    def _broadcast_channel(
        self,
        channel: Channel,
        line: str,
        exclude: ClientSession | None = None,
    ) -> None:
        for member in list(channel.members):
            if member is exclude or member.closed:
                continue
            self._queue_line(member, line)

    def _send_names(self, session: ClientSession, channel: Channel) -> None:
        names = sorted((m.nickname or "*") for m in channel.members)
        base_params = ("=", channel.name)

        current: list[str] = []
        for name in names:
            candidate = current + [name]
            line = format_irc(
                SERVER_NAME,
                "353",
                self._target(session),
                *base_params,
                " ".join(candidate),
            )
            if len(line.encode("utf-8")) + 2 > MAX_IRC_LINE_BYTES and current:
                self._numeric(session, "353", *base_params, " ".join(current))
                current = [name]
            else:
                current = candidate

        if current:
            self._numeric(session, "353", *base_params, " ".join(current))
        self._numeric(session, "366", channel.name, "End of NAMES list")

    def _dispatch(self, session: ClientSession, msg: IRCMessage) -> None:
        handlers = {
            "CAP": self._cmd_cap,
            "NICK": self._cmd_nick,
            "USER": self._cmd_user,
            "PING": self._cmd_ping,
            "PONG": self._cmd_pong,
            "QUIT": self._cmd_quit,
            "JOIN": self._cmd_join,
            "PART": self._cmd_part,
            "NAMES": self._cmd_names,
            "PRIVMSG": self._cmd_privmsg,
            "NOTICE": self._cmd_notice,
            "MODE": self._cmd_mode,
            "WHO": self._cmd_who,
        }
        handler = handlers.get(msg.command)
        if handler is None:
            self._numeric(session, "421", msg.command, "Unknown command")
            return
        handler(session, msg.params)

    def _cmd_cap(self, session: ClientSession, params: tuple[str, ...]) -> None:
        if not params:
            return
        sub = params[0].upper()
        if sub == "LS":
            self._queue_line(session, format_irc(SERVER_NAME, "CAP", "*", "LS", ""))
        elif sub == "REQ":
            requested = params[-1]
            self._queue_line(session, format_irc(SERVER_NAME, "CAP", "*", "NAK", requested))

    def _cmd_nick(self, session: ClientSession, params: tuple[str, ...]) -> None:
        if not params or not params[0]:
            self._numeric(session, "431", "No nickname given")
            return

        new_nick = params[0]
        if not NICK_RE.fullmatch(new_nick):
            self._numeric(session, "432", new_nick, "Erroneous nickname")
            return

        key = irc_casefold(new_nick)
        owner = self.nick_index.get(key)
        if owner is not None and owner is not session:
            self._numeric(session, "433", new_nick, "Nickname is already in use")
            return

        old_nick = session.nickname
        old_prefix = session.prefix()
        if old_nick is not None:
            self.nick_index.pop(irc_casefold(old_nick), None)

        session.nickname = new_nick
        self.nick_index[key] = session

        if session.registered and old_nick is not None:
            line = format_irc(old_prefix, "NICK", new_nick)
            recipients = self._shared_peers(session) | {session}
            for recipient in recipients:
                self._queue_line(recipient, line)
        else:
            self._try_finish_registration(session)

    def _cmd_user(self, session: ClientSession, params: tuple[str, ...]) -> None:
        if session.registered or session.username is not None:
            self._numeric(session, "462", "You may not reregister")
            return
        if len(params) < 4:
            self._numeric(session, "461", "USER", "Not enough parameters")
            return

        session.username = params[0][:32]
        session.realname = params[3][:128]
        self._try_finish_registration(session)

    def _cmd_ping(self, session: ClientSession, params: tuple[str, ...]) -> None:
        if not params:
            self._numeric(session, "409", "No origin specified")
            return
        token = params[-1]
        self._queue_line(session, format_irc(SERVER_NAME, "PONG", SERVER_NAME, token))

    def _cmd_pong(self, session: ClientSession, _params: tuple[str, ...]) -> None:
        session.ping_token = None
        session.ping_sent_at = None

    def _cmd_quit(self, session: ClientSession, params: tuple[str, ...]) -> None:
        reason = params[0] if params else "Client Quit"
        self._disconnect(session, reason)

    def _cmd_join(self, session: ClientSession, params: tuple[str, ...]) -> None:
        if not self._require_registered(session):
            return
        if not params or not params[0]:
            self._numeric(session, "461", "JOIN", "Not enough parameters")
            return

        for requested in params[0].split(","):
            if not valid_channel_name(requested):
                self._numeric(session, "476", requested or "*", "Bad Channel Mask")
                continue

            key = irc_casefold(requested)
            channel = self.channels.get(key)
            if channel is None:
                channel = Channel(name=requested)
                self.channels[key] = channel

            if session in channel.members:
                continue

            channel.members.add(session)
            session.channels.add(key)

            join_line = format_irc(session.prefix(), "JOIN", channel.name)
            self._broadcast_channel(channel, join_line)
            self._numeric(session, "331", channel.name, "No topic is set")
            self._send_names(session, channel)

    def _cmd_part(self, session: ClientSession, params: tuple[str, ...]) -> None:
        if not self._require_registered(session):
            return
        if not params or not params[0]:
            self._numeric(session, "461", "PART", "Not enough parameters")
            return

        reason = params[1] if len(params) > 1 else session.nickname or "leaving"
        for requested in params[0].split(","):
            key = irc_casefold(requested)
            channel = self.channels.get(key)
            if channel is None:
                self._numeric(session, "403", requested, "No such channel")
                continue
            if session not in channel.members:
                self._numeric(session, "442", channel.name, "You're not on that channel")
                continue

            line = self._bounded_line(session.prefix(), "PART", channel.name, reason)
            self._broadcast_channel(channel, line)
            channel.members.discard(session)
            session.channels.discard(key)
            if not channel.members:
                self.channels.pop(key, None)

    def _cmd_names(self, session: ClientSession, params: tuple[str, ...]) -> None:
        if not self._require_registered(session):
            return

        if not params or not params[0]:
            for channel in list(self.channels.values()):
                self._send_names(session, channel)
            return

        for requested in params[0].split(","):
            requested_channel = self.channels.get(irc_casefold(requested))
            if requested_channel is None:
                self._numeric(session, "366", requested, "End of NAMES list")
            else:
                self._send_names(session, requested_channel)

    def _cmd_privmsg(self, session: ClientSession, params: tuple[str, ...]) -> None:
        if not self._require_registered(session):
            return
        if not params or not params[0]:
            self._numeric(session, "411", "No recipient given (PRIVMSG)")
            return
        if len(params) < 2 or params[1] == "":
            self._numeric(session, "412", "No text to send")
            return

        text = params[1]
        for target in params[0].split(","):
            self._route_message(session, target, text, command="PRIVMSG", errors=True)

    def _cmd_notice(self, session: ClientSession, params: tuple[str, ...]) -> None:
        if not session.registered or len(params) < 2:
            return
        text = params[1]
        for target in params[0].split(","):
            self._route_message(session, target, text, command="NOTICE", errors=False)

    def _route_message(
        self,
        sender: ClientSession,
        target: str,
        text: str,
        command: str,
        errors: bool,
    ) -> None:
        if target.startswith("#"):
            channel = self.channels.get(irc_casefold(target))
            if channel is None:
                if errors:
                    self._numeric(sender, "403", target, "No such channel")
                return
            if sender not in channel.members:
                if errors:
                    self._numeric(sender, "404", channel.name, "Cannot send to channel")
                return
            line = self._forwarded_message(sender, command, channel.name, text)
            self._broadcast_channel(channel, line, exclude=sender)
            return

        recipient = self.nick_index.get(irc_casefold(target))
        if recipient is None or not recipient.registered:
            if errors:
                self._numeric(sender, "401", target, "No such nick/channel")
            return
        line = self._forwarded_message(sender, command, recipient.nickname or target, text)
        self._queue_line(recipient, line)

    def _cmd_mode(self, session: ClientSession, params: tuple[str, ...]) -> None:
        if not self._require_registered(session) or not params:
            return
        target = params[0]
        if target.startswith("#"):
            channel = self.channels.get(irc_casefold(target))
            if channel is None:
                self._numeric(session, "403", target, "No such channel")
                return
            if len(params) == 1:
                self._numeric(session, "324", channel.name, "+")
        elif session.nickname and irc_casefold(target) == irc_casefold(session.nickname):
            if len(params) == 1:
                self._numeric(session, "221", "+")

    def _cmd_who(self, session: ClientSession, params: tuple[str, ...]) -> None:
        if not self._require_registered(session):
            return
        mask = params[0] if params else "*"
        channel = self.channels.get(irc_casefold(mask)) if mask.startswith("#") else None
        members = list(channel.members) if channel else []
        for member in members:
            self._numeric(
                session,
                "352",
                channel.name if channel else "*",
                member.username or "unknown",
                member.host,
                SERVER_NAME,
                member.nickname or "*",
                "H",
                f"0 {member.realname or member.nickname or ''}",
            )
        self._numeric(session, "315", mask, "End of WHO list")

    def _check_liveness(self) -> None:
        now = time.monotonic()
        for session in list(self.sessions.values()):
            if session.closed:
                continue

            if session.ping_token is not None:
                if (
                    session.ping_sent_at is not None
                    and now - session.ping_sent_at >= DROP_AFTER_PING
                ):
                    logging.info("[%s] timed out waiting for PONG", session.id)
                    self._disconnect(session, "Ping timeout")
                continue

            if now - session.last_activity >= PING_AFTER:
                token = uuid.uuid4().hex[:12]
                session.ping_token = token
                session.ping_sent_at = now
                self._queue_line(session, format_irc(None, "PING", token))

    def _disconnect(self, session: ClientSession, reason: str, announce: bool = True) -> None:
        if session.closed:
            return

        if announce and session.registered and session.nickname:
            quit_line = self._bounded_line(session.prefix(), "QUIT", reason)
            for peer in self._shared_peers(session):
                self._queue_line(peer, quit_line)

        for key in list(session.channels):
            channel = self.channels.get(key)
            if channel:
                channel.members.discard(session)
                if not channel.members:
                    self.channels.pop(key, None)
        session.channels.clear()

        if session.nickname:
            self.nick_index.pop(irc_casefold(session.nickname), None)

        session.closed = True
        self.sessions.pop(session.sock, None)
        try:
            self.selector.unregister(session.sock)
        except (KeyError, ValueError):
            logging.debug("[%s] socket was already unregistered", session.id)
        try:
            session.sock.close()
        except OSError as exc:
            logging.debug("[%s] socket close failed: %s", session.id, exc)

        logging.info("[%s] disconnected: %s", session.id, reason)


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the server command-line parser."""
    parser = argparse.ArgumentParser(description="Minimal IPv6 IRC server for the coursework")
    parser.add_argument("--host", default="::", help="IPv6 bind address (default: ::)")
    parser.add_argument("--port", type=int, default=6667, help="TCP port (default: 6667)")
    parser.add_argument("--debug", action="store_true", help="enable verbose protocol logging")
    return parser


def main() -> None:
    """Configure logging and run the server."""
    args = build_arg_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    server = IRCServer(host=args.host, port=args.port)
    server.start()


if __name__ == "__main__":
    main()
