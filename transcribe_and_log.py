#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Tom Salzer, KJ7T
# Full license text: see LICENSE-MIT in the repository root.
#
# This file is original work and does not import upstream KK7NQN code. It is
# distributed here as part of a GPLv3 project, but is additionally available
# under the MIT license for independent reuse.
"""
transcribe_and_log.py — Whisper transcription + DB insert for one WAV file.

Not part of Hunter's published KK7NQN-TranscriptionLogger repo (his
transcribe_watcher.py calls this filename, but the script itself wasn't
included) — this is our own implementation of that missing piece, using
faster-whisper and matching the `transcriptions` table schema from
Database/MainDatabase.sql so it stays compatible with his AI_Scripts/.

Invoked by transcribe_watcher.py as:
    python3 transcribe_and_log.py /path/to/recording.wav

Exit code 0 = success (watcher moves file to processed/)
Exit code nonzero = failure (watcher moves file to failed/)

Env vars:
  DB_HOST, DB_USER, DB_PASS, DB_NAME    -- MariaDB connection (repeater DB)
  WHISPER_MODEL        (default "small")
  WHISPER_COMPUTE_TYPE (default "int8") -- CPU-friendly quantization
"""
import os
import sys
import re
import datetime
import mysql.connector
from faster_whisper import WhisperModel

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "user": os.getenv("DB_USER", "transcriber"),
    "password": os.getenv("DB_PASS", ""),
    "database": os.getenv("DB_NAME", "repeater"),
}

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

# Matches ami_ptt_recorder.py's naming: 588416_20260830-105635_SimpleUSB-588416.wav
FILENAME_TS_RE = re.compile(r"_(\d{8}-\d{6})_")

_model = None


def get_model():
    global _model
    if _model is None:
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type=WHISPER_COMPUTE_TYPE)
    return _model


def parse_timestamp(filename: str) -> datetime.datetime:
    m = FILENAME_TS_RE.search(filename)
    if m:
        try:
            return datetime.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
        except ValueError:
            pass
    return datetime.datetime.now()


def transcribe(path: str) -> str:
    model = get_model()
    segments, _info = model.transcribe(
        path,
        language="en",
        vad_filter=False,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def insert_transcription(filename: str, text: str, ts: datetime.datetime) -> None:
    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transcriptions (filename, transcription, timestamp, processed, analyzed) "
        "VALUES (%s, %s, %s, 0, 0)",
        (filename, text, ts),
    )
    conn.commit()
    cur.close()
    conn.close()


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: transcribe_and_log.py <wav_path>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    filename = os.path.basename(path)
    ts = parse_timestamp(filename)

    text = transcribe(path)
    if not text:
        print(f"[WARN] Empty transcription for {filename} (silence or noise-only clip)", flush=True)

    insert_transcription(filename, text, ts)
    print(f"[OK] {filename} -> {len(text)} chars logged at {ts}", flush=True)


if __name__ == "__main__":
    main()
