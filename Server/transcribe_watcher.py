#!/usr/bin/env python3
"""
transcribe_watcher.py — polls the incoming recordings directory and hands each
finished WAV to transcribe_and_log.py.

Modified 2026-09-01 by Tom Salzer (KJ7T):
  - Only process a file once its size has been stable across two polls. The
    recorder now stages files and moves them in complete, so this is belt and
    braces — but it makes the watcher correct on its own, including for anyone
    running the original recorder that writes straight into this directory.
  - [SKIP] now reports isfile and size. "invalid or empty" with no numbers is
    what made a 16%-data-loss bug take an hour to diagnose.
  - flush=True on all prints, so journal timestamps are event times rather than
    buffer-flush times. Also set PYTHONUNBUFFERED=1 in the systemd unit.

Original work Copyright (c) Hunter Inman (KK7NQN), licensed under GPLv3.
"""
import os
import time
import subprocess

BASE = "/opt/allstar-transcriber/recordings"
WATCH_DIR = f"{BASE}/incoming"
PROCESSED_DIR = f"{BASE}/processed"
ERROR_DIR = f"{BASE}/failed"
TRANSCRIBE_SCRIPT = "/opt/allstar-transcriber/transcribe_and_log.py"
PYTHON_BIN = "/opt/allstar-transcriber/venv/bin/python3"

POLL_SECONDS = 5
MIN_VALID_BYTES = 1000

os.makedirs(PROCESSED_DIR, exist_ok=True)
os.makedirs(ERROR_DIR, exist_ok=True)

# filename -> size seen on the previous poll, used to detect files still growing
last_sizes: dict[str, int] = {}


def say(msg: str) -> None:
    print(msg, flush=True)


while True:
    try:
        files = [f for f in os.listdir(WATCH_DIR) if f.lower().endswith(".wav")]
    except OSError as e:
        say(f"[WARN] cannot list {WATCH_DIR}: {e}")
        time.sleep(POLL_SECONDS)
        continue

    # Drop remembered sizes for files that are gone, so the dict cannot grow
    # without bound over a long run.
    for gone in set(last_sizes) - set(files):
        last_sizes.pop(gone, None)

    for f in files:
        full_path = os.path.join(WATCH_DIR, f)

        try:
            size = os.path.getsize(full_path)
        except OSError as e:
            say(f"[WARN] cannot stat {f}: {e}")
            continue

        previous = last_sizes.get(f)
        last_sizes[f] = size

        # Wait for a file to hold the same size across two consecutive polls
        # before touching it. A recording still being written by Asterisk will
        # keep growing, and acting on it moves the file out from under an open
        # file descriptor.
        if previous is None or size != previous:
            continue

        if size <= MIN_VALID_BYTES:
            say(f"[SKIP] {f} is invalid or empty "
                f"(size={size} bytes, threshold={MIN_VALID_BYTES}).")
            try:
                os.rename(full_path, os.path.join(ERROR_DIR, f))
            except Exception as e:
                say(f"[WARN] could not move invalid file {f}: {e}")
            last_sizes.pop(f, None)
            continue

        say(f"[INFO] Processing {f} ({size} bytes)")
        try:
            subprocess.run(
                [PYTHON_BIN, TRANSCRIBE_SCRIPT, full_path],
                check=True,
                timeout=1500,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            say(f"[SUCCESS] {f} processed.")
            os.rename(full_path, os.path.join(PROCESSED_DIR, f))

        except subprocess.TimeoutExpired:
            say(f"[TIMEOUT] {f} took too long, moving to failed.")
            os.rename(full_path, os.path.join(ERROR_DIR, f))

        except subprocess.CalledProcessError as e:
            say(f"[ERROR] Failed to process {f}")
            say("  STDOUT: " + (e.stdout.decode(errors="ignore") if e.stdout else "(empty)"))
            say("  STDERR: " + (e.stderr.decode(errors="ignore") if e.stderr else "(empty)"))
            os.rename(full_path, os.path.join(ERROR_DIR, f))

        except Exception as e:
            say(f"[UNEXPECTED] Error with {f}: {e}")
            os.rename(full_path, os.path.join(ERROR_DIR, f))

        last_sizes.pop(f, None)

    time.sleep(POLL_SECONDS)
