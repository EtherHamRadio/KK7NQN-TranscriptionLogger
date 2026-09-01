#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Tom Salzer, KJ7T
# Full license text: see LICENSE-MIT in the repository root.
#
# This file is original work and does not import upstream KK7NQN code. It is
# distributed here as part of a GPLv3 project, but is additionally available
# under the MIT license for independent reuse.
"""
ami_ptt_recorder.py — AMI-driven PTT/COS recorder for an AllStarLink hub node.

Built on the same event-parsing approach as KK7NQN-TranscriptionLogger's
cosptt_logger.py (https://github.com/Wintergrasped/KK7NQN-TranscriptionLogger),
but instead of only logging rxkeyed/txkeyed transitions to a text file, this
issues real AMI MixMonitor/StopMixMonitor actions so each transmission lands as
its own WAV file — ready for a local watcher to hand off to Whisper.

CONFIRMED against a live event dump on node 588416 (2026-08-30): this hub
never emits an rxkeyed-flavored event at all (no physical receiver, so no
COS). What actually fires when a linked node keys up is RPT_ALINKS/RPT_LINKS
(reporting which link is transmitting into the hub), followed by
RPT_TXKEYED going to EventValue: 1 on a stable Channel: SimpleUSB/<node>,
then the mirror-image sequence back to EventValue: 0 on unkey. Each of these
also shows up a second time as a generic VarSet event with the same
Variable/Value — harmless, since start_recording()/stop_recording() below
are idempotent per channel, so the duplicate is a no-op.

So the trigger here is txkeyed, not rxkeyed — recording starts when the hub
itself starts (re-)transmitting the incoming audio, which is the point at
which speech is actually flowing.

STAGING (added 2026-09-01, KJ7T)
--------------------------------
MixMonitor now writes into STAGING_DIR, not RECORD_DIR, and the finished file
is moved into RECORD_DIR a couple of seconds after StopMixMonitor.

Why: previously MixMonitor wrote directly into the directory the transcription
watcher polls. The watcher checks file size to decide whether a WAV is valid,
and a recording caught in its first moments is only a few hundred bytes — so
roughly one transmission in six was declared "invalid or empty" and moved to
failed/ while Asterisk was still writing to it. (Asterisk's file descriptor
follows the inode, so those files kept growing in failed/ — which is why they
ended up multi-megabyte despite having been rejected as empty.) Measured on
node 588416: 209 of 1308 captured files, about 16%, lost this way.

Staging removes the race: the watcher only ever sees files that are complete
and closed. STAGING_DIR and RECORD_DIR should be on the same filesystem so the
move is an atomic rename.

Env vars:
  AMI_HOST         (default 127.0.0.1)
  AMI_PORT         (default 5038)
  AMI_USER         (required)
  AMI_PASS         (required)
  RECORD_DIR       (default /var/spool/asterisk/recordings/588416/incoming)
                   Where finished WAVs are delivered; this is what the watcher polls.
  STAGING_DIR      (default: sibling "staging" dir next to RECORD_DIR)
                   Where MixMonitor actually writes. Same filesystem as RECORD_DIR.
  FINALIZE_DELAY   (default 2.0) seconds to wait after StopMixMonitor before
                   moving the file, giving Asterisk time to close it cleanly.
  NODE_NUM         (default 588416) — cosmetic, used in filenames/logs only
  AMI_DEBUG        (default 0) — set to 1 to print every raw AMI event block
  LOG_FILE         (default /opt/allstar-transcriber/logs/ptt_recorder.log)

Run manually first (recommended for the first test):
  AMI_USER=youruser AMI_PASS=yourpass AMI_DEBUG=1 python3 ami_ptt_recorder.py

Then wire it into systemd once verified. Set PYTHONUNBUFFERED=1 in the unit —
without it Python block-buffers stdout when systemd captures it, and every
timestamp in the journal is a flush time rather than an event time.
"""

from __future__ import annotations
import os
import re
import shutil
import socket
import sys
import threading
import datetime
from pathlib import Path

AMI_HOST = os.getenv("AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.getenv("AMI_PORT", "5038"))
AMI_USER = os.getenv("AMI_USER", "")
AMI_PASS = os.getenv("AMI_PASS", "")
RECORD_DIR = Path(os.getenv("RECORD_DIR", "/var/spool/asterisk/recordings/588416/incoming"))
STAGING_DIR = Path(os.getenv("STAGING_DIR", str(RECORD_DIR.parent / "staging")))
FINALIZE_DELAY = float(os.getenv("FINALIZE_DELAY", "2.0"))
NODE_NUM = os.getenv("NODE_NUM", "588416")
AMI_DEBUG = os.getenv("AMI_DEBUG", "0") == "1"
LOG_FILE = os.getenv("LOG_FILE", "/opt/allstar-transcriber/logs/ptt_recorder.log")

# Reused from cosptt_logger.py's detection approach.
CHANNEL_RE = re.compile(r"^Channel:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

# Tracks in-progress recordings so we can StopMixMonitor the right channel.
active_recordings: dict[str, str] = {}  # channel -> staging file path


def log(message: str) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"{now} | {message}"
    print(entry, flush=True)
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(entry + "\n")
    except OSError as e:
        print(f"[WARN] could not write log file {LOG_FILE}: {e}", flush=True)


def debug(msg: str) -> None:
    if AMI_DEBUG:
        print(f"[DEBUG] {msg}", flush=True)


def safe_channel_tag(channel: str) -> str:
    """Turn an AMI channel name into something filesystem-safe."""
    return re.sub(r"[^A-Za-z0-9]+", "-", channel).strip("-")[:80]


def send_ami(sock: socket.socket, action_lines: list[str]) -> None:
    payload = "\r\n".join(action_lines) + "\r\n\r\n"
    sock.sendall(payload.encode())


def finalize_recording(staged_path: str) -> None:
    """Move a finished recording from STAGING_DIR into RECORD_DIR.

    Runs on a timer thread FINALIZE_DELAY seconds after StopMixMonitor, so
    Asterisk has closed the file before the watcher can see it. os.replace is
    atomic when both paths are on the same filesystem; shutil.move is the
    fallback if they have been configured across a boundary.
    """
    src = Path(staged_path)
    dst = RECORD_DIR / src.name
    try:
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, dst)
        except OSError:
            # Cross-filesystem: fall back to copy+delete. Not atomic, so keep
            # STAGING_DIR and RECORD_DIR on the same filesystem if you can.
            shutil.move(str(src), str(dst))
        size = dst.stat().st_size if dst.exists() else -1
        log(f"FINALIZE {src.name} -> {RECORD_DIR} ({size} bytes)")
    except FileNotFoundError:
        log(f"WARN finalize: {src} not found — recording may never have started")
    except OSError as e:
        log(f"WARN finalize: could not move {src} -> {dst}: {e}")


def start_recording(sock: socket.socket, channel: str) -> None:
    if channel in active_recordings:
        return  # already recording this channel
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name = f"{NODE_NUM}_{ts}_{safe_channel_tag(channel)}"
    # MixMonitor's File param wants the extension included — format is inferred from it.
    file_path = str(STAGING_DIR / f"{base_name}.wav")
    send_ami(sock, [
        "Action: MixMonitor",
        f"Channel: {channel}",
        f"File: {file_path}",
    ])
    active_recordings[channel] = file_path
    log(f"RECORD START channel={channel} file={file_path}")


def stop_recording(sock: socket.socket, channel: str) -> None:
    file_path = active_recordings.pop(channel, None)
    if file_path is None:
        return  # we weren't recording this one (e.g. started before we connected)
    send_ami(sock, [
        "Action: StopMixMonitor",
        f"Channel: {channel}",
    ])
    log(f"RECORD STOP  channel={channel} file={file_path}")
    # Defer the move so Asterisk can finish closing the file. A timer thread
    # keeps the AMI read loop unblocked; there is at most one per transmission
    # and each exits immediately after the move.
    t = threading.Timer(FINALIZE_DELAY, finalize_recording, args=(file_path,))
    t.daemon = True
    t.start()


def handle_event(sock: socket.socket, event: str) -> None:
    debug(f"Received event block:\n{event}\n---")
    lower_event = event.lower()

    # Surface AMI errors in the persistent log, not just ephemeral AMI_DEBUG output —
    # this is exactly what would have made the Monitor/StopMonitor failure obvious
    # immediately instead of requiring a scrollback hunt.
    if "response: error" in lower_event:
        first_line = event.strip().splitlines()[0] if event.strip() else event
        log(f"AMI ERROR: {first_line} | {event.strip()}".replace("\n", " / "))

    m = CHANNEL_RE.search(event)
    channel = m.group(1).strip() if m else None

    if "txkeyed" in lower_event:
        if "value: 1" in lower_event:
            if channel:
                start_recording(sock, channel)
            else:
                log("WARN txkeyed=1 event had no Channel header — cannot record. "
                    "Check AMI_DEBUG output and adjust CHANNEL_RE.")
        elif "value: 0" in lower_event:
            if channel:
                stop_recording(sock, channel)


def recover_orphans() -> None:
    """Deliver anything left in STAGING_DIR from a previous run.

    If the process died between MixMonitor and StopMixMonitor, a WAV can be
    stranded in staging. Anything already there at startup is by definition not
    being written by this process, so it is safe to move across.
    """
    if not STAGING_DIR.is_dir():
        return
    for p in sorted(STAGING_DIR.glob("*.wav")):
        log(f"ORPHAN recovered from staging: {p.name}")
        finalize_recording(str(p))


def main() -> None:
    if not AMI_USER or not AMI_PASS:
        print("AMI_USER and AMI_PASS must be set.", file=sys.stderr, flush=True)
        sys.exit(1)

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    log(f"staging={STAGING_DIR} record={RECORD_DIR} finalize_delay={FINALIZE_DELAY}s")
    recover_orphans()

    debug(f"Connecting to AMI at {AMI_HOST}:{AMI_PORT} ...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((AMI_HOST, AMI_PORT))
    debug("Connected. Logging in...")

    sock.sendall(
        f"Action: Login\r\nUsername: {AMI_USER}\r\nSecret: {AMI_PASS}\r\n\r\n".encode()
    )
    sock.sendall(b"Action: Events\r\nEventMask: on\r\n\r\n")

    buffer = ""
    login_confirmed = False
    while True:
        data = sock.recv(4096).decode(errors="ignore")
        if not data:
            log("Connection closed by Asterisk.")
            break
        buffer += data
        if "\r\n\r\n" not in buffer:
            continue

        events = buffer.split("\r\n\r\n")
        buffer = events.pop()

        for event in events:
            if not login_confirmed:
                if "response: success" in event.lower():
                    login_confirmed = True
                    log(f"AMI login OK as {AMI_USER}")
                elif "response: error" in event.lower():
                    log(f"AMI login FAILED: {event}")
                    sys.exit(1)
                continue
            handle_event(sock, event)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting on Ctrl+C", flush=True)
