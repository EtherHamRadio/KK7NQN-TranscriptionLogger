# Changes from upstream

Modifications to [KK7NQN-TranscriptionLogger](https://github.com/Wintergrasped/KK7NQN-TranscriptionLogger) by Tom Salzer (KJ7T), August–September 2026.

Each was found by running the pipeline against live net traffic on AllStarLink hub node 588416 — five captures across four repeaters. Where a fix changes behaviour, the evidence is recorded so you can judge whether it applies to your installation.

---

## `Transcript_Analyzer.py` (GPLv3, upstream file, modified)

### Sessions were scored in fragments and written off permanently

`fetch_transcripts_for_analysis()` selects only transcripts with no `transcription_analysis` row, plus a narrow rescan case limited to `is_net=1` rows missing a name. Once a transcript is scored `is_net=0` it is never fetched again.

Combined with a five-minute timer, that means each run sees only the transcripts that arrived since the last run and hands *just those* to `make_sessions()`, scoring the result as though it were a complete session. A 97-minute net gets chopped into roughly five-minute fragments, each evaluated in isolation, each scoring below threshold, each written off permanently.

Observed on a real net: the opening fragment contained "welcome to the 9 o'clock net" and scored 0.7 — the phrase hit plus an on-the-hour bonus, with no duration bonus (the fragment was under four minutes) and too few callsigns. Every later fragment contained the participants but no opening language and scored 0.0. The evidence for "this is a net" was all present in the data; it was never allowed to accumulate in one place.

**Fix:** in `main()`, skip any session whose last transcript is still within `SESSION_GAP_MIN` of now. Deferred transcripts get no analysis row, so they remain eligible under the existing `WHERE ta.transcription_id IS NULL` clause and the session keeps growing across runs until it goes quiet, then is scored once, whole.

**Trade-off:** a net is detected roughly ten minutes after it ends rather than during. For a logging tool that is a good trade.

**Verified live:** across a 90-minute net the batch grew 3 → 20 → 68 → 139 → 202 transcripts with a `Deferring 1 still-active session(s)` line each run, then finalised once, eleven minutes after the last transmission.

### The callsign regex silently dropped valid prefixes

```python
CALLSIGN_RE = re.compile(r"\b([A-KN-PR-Z]{1,2}\d{1,4}[A-Z]{1,3})...")
```

That prefix class is A–K, N–P, R–Z. It excludes **L, M and Q**. Q is correct — no amateur callsign begins with Q — but L and M are legitimate US prefix letters. KL7 (Alaska), KM7, WL, AL, NL and NM were all invisible.

Real cost: `KM7ETI`, `KM7HHA`, `KM7GSG` and `KM7BJX` were missed across two captures. This suppresses participant counts *and* the ≥12-callsign scoring bonus.

**Fix:** `[A-PR-Z]`, which restores L and M while still excluding Q.

### Phrase hits were multiplied by occurrence count

`add_hits()` did `score += weight * count`. A net where control says "in and out" twelve times scored 3.6 on that phrase alone.

That makes `confidence_score` meaningless as a confidence measure — a chatty net outscores a formally-run one on repetition — and it means adding any common phrase to the lists risks turning ordinary conversation into a detected net.

**Fix:** a `max_credit` parameter (default 1) caps each phrase's contribution while `keyword_hits` still records the true count for diagnostics.

**Measured:** on one 249-transmission net the uncapped total would have been about 6.5 against a threshold of 1.0. Capped, it scored 1.7.

### Weak phrase list extended

Added `check(ing)?-?in(s)?`, `in and out`, and `for the log` — net jargon that essentially never appears in ordinary ragchew, chosen from operator knowledge and then tested.

**Result on a net where the opening announcement was missed entirely** (joined six minutes late, no on-the-hour bonus, callsign count under 12): detected at 1.1, carried by the check-in phrase plus session duration. Under the original phrase list the same net scored 0.8 and would have been missed.

**Honest caveat:** on that same capture, `in and out` and `for the log` did not fire at all. Two of three additions contributed nothing. Phrase tuning is repeater-specific and probabilistic.

### Detection threshold made configurable

`NET_SCORE_THRESHOLD` (default 1.0) replaces the hardcoded value in `main()`. Net loggers and repeater archivists want opposite error biases; this lets each choose without editing source.

### Hardcoded credential removed

`DB_PASS` had a real password as its `os.getenv` fallback. Now defaults to empty.

---

## `transcribe_watcher.py` (GPLv3, upstream file, modified)

### Invalid files were never moved out of `incoming/`

A WAV failing the validity check was logged as `[SKIP] ... is invalid or empty` and left in place, to be re-checked and re-logged every five-second poll indefinitely.

**Fix:** move it to `failed/`.

### Files were acted on while still being written

See the recorder section below — the watcher was the other half of that race.

**Fix:** a file must hold the same size across two consecutive polls before it is touched. This makes the watcher safe on its own, including for anyone running the unpatched recorder that writes straight into the watched directory. Costs one poll interval of latency.

### `[SKIP]` now reports what it saw

The message now includes `size=` and the threshold. "Invalid or empty" with no numbers is what made a 16%-data-loss bug take an hour to diagnose.

### Unbuffered output

`flush=True` on all prints. Set `PYTHONUNBUFFERED=1` in the unit as well.

---

## `ami_ptt_recorder.py` (MIT, new file)

Upstream's `cosptt_logger.py` logs COS/PTT transitions to a text file but never records audio. This issues real AMI MixMonitor/StopMixMonitor actions so each transmission lands as its own WAV.

On a hub node with no physical receiver there is no COS and no rxkeyed event at all. The working trigger is `RPT_TXKEYED` on a stable `SimpleUSB/<node>` channel — recording starts when the hub begins retransmitting, which is when speech is actually flowing.

### Staging directory (the 16% fix)

MixMonitor originally wrote directly into the directory the watcher polls. The watcher's size check would catch a recording in its first moments — a few hundred bytes, header barely written — declare it empty, and move it to `failed/` while Asterisk was still writing. Asterisk's file descriptor follows the inode rather than the path, so those files went on growing in `failed/`, which is why they ended up multi-megabyte despite having been rejected as empty.

**Measured on node 588416: 209 of 1308 files, about 16% of all captured audio, silently discarded.**

The signature that identified it, once buffered logging was fixed and timestamps became trustworthy:

```
[SKIP]    ...061543... is invalid or empty.
[SUCCESS] ...061542... processed.
[SKIP]    ...061738... is invalid or empty.
[SUCCESS] ...061739... processed.
```

Files recorded one second apart, opposite fates, differing only in which the poll happened to catch mid-write.

**Fix:** MixMonitor writes to `STAGING_DIR`; the finished file is moved into `RECORD_DIR` `FINALIZE_DELAY` seconds (default 2.0) after `StopMixMonitor`, on a timer thread so the AMI read loop never blocks. Same filesystem, so `os.replace` is atomic. The watcher only ever sees complete, closed files.

### Orphan recovery

Anything left in `STAGING_DIR` at startup is delivered to `RECORD_DIR`. If the process died between start and stop, that recording is no longer lost.

---

## `netviewer.py` (MIT, new file)

Web UI on port 8088 for querying transcripts by time range.

- **Download .txt** — plain text, one transmission per line, header giving the window, transmission count and any detected net. Blank transmissions counted but omitted. Formatted for an LLM prompt rather than a spreadsheet.
- **Net rename** — edit a detected net's name inline; updates `net_data` and every matching `transcription_analysis` row. Added because speech-derived names are unreliable and because one repeater running three nets a day produces name collisions.

No authentication. One write path. Do not expose it beyond a VPN.

---

## `transcribe_and_log.py` (MIT, new file)

Referenced by the upstream README but not included in the repo.

**`vad_filter=False`** is deliberate. faster-whisper's Silero VAD pre-filter, at default sensitivity, discarded real speech on this node's weak RF audio — 73 of 231 transmissions came back blank, and 54 of those were 1.5 seconds or longer, several running 5–7 seconds with clearly audible speech.

An A/B test on four representative "blank but long" clips recovered full sentences with the filter off. An intermediate attempt at loosening the threshold (0.35, with adjusted speech and silence durations) recovered only fragments and was not enough.

Whisper's own `no_speech_threshold`, `log_prob_threshold` and `compression_ratio_threshold` remain active.

---

## Still open

- `start_recording()` has no maximum duration. If a `txkeyed value: 0` event is missed, the channel stays in `active_recordings` indefinitely — MixMonitor keeps writing one growing file and every subsequent transmission on that channel is silently dropped. A duration cap would bound the damage.
- `extract_ncs()` requires a "this is *callsign* … net control" construction. Neither repeater tested actually talks that way, so `ncs_callsign` has been NULL on every net detected.
- `detect_topics()` includes the bare word `"for"` in its Swap keyword list, so nearly every transcript is tagged swap-and-shop. Harmless while nothing reads `topic_labels`.
- Net naming needs a node-and-schedule lookup rather than speech extraction.
- The AI refinement path is untested here. `_compact_rows()` in `ai_backend.py` filters out transmissions lacking a keyword or regex-matchable callsign before the model sees them, discarding the phonetic spellouts a model handles best. `should_call_ai()` only fires when the regex failed to produce a *name*, so a well-scoring net never reaches the model at all.
