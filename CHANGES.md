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

Upstream's `cosppt_logger.py` logs COS/PTT transitions to a text file but never records audio. This issues real AMI MixMonitor/StopMixMonitor actions so each transmission lands as its own WAV.

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

### Model choice: `small` + `int8`, and why not larger

Callsign accuracy is the weakest part of this pipeline, so the obvious move is a bigger Whisper model. It was benchmarked against real captured audio and rejected. Three separate attempts to make the transcriber smarter each made callsigns **worse**:

| Attempt | Result on real audio |
|---|---|
| `medium` instead of `small` | 2.9× slower (median 16.7 s vs 5.8 s per transmission). Rendered "foxtrot zero" as **"Flaxstrad 0"** and "Caramel Business" as **"criminal business"** |
| `initial_prompt` with the phonetic alphabet and known callsigns | Emitted **"KJ7T-OR."** — a callsign lifted straight out of the primer — on a clip containing no intelligible speech |
| `hotwords` with a callsign list | Mangled the phonetics into **"KiloFoxTrad0"** and **"MIK9 SIERRA ALFAYANKI"** |

Across seven callsign-bearing clips, `medium` won one and lost one against `small`, with five ties — for triple the CPU.

The explanation is that a larger language model has a stronger prior about what English sounds like, and the phonetic alphabet is *deliberately* built from words that do not sound like ordinary English. The prior fights the vocabulary. Priming makes it worse still, because a model told to expect callsigns will produce one when it hears nothing at all.

`small` transcribes phonetics literally — "Whiskey Bravo 3, Charlie, Sierra Yankee" — which looks like a failure and is in fact the most useful output available, because a lookup table converts it deterministically. See `callsign_resolver.py`.

**If you want to reproduce this**, benchmark on your own audio before switching models. The result may differ on cleaner RF.

---

## `callsign_resolver.py` (MIT, new file)

A post-processing pass that turns spoken phonetics into callsigns. It reads text already in `transcriptions`, writes to its own three tables, and **never modifies a transcript or touches the upstream schema**. Not in the capture or transcription path.

Motivated by the benchmark above: the fix for bad callsign transcription turned out to be a lookup table, not a better model.

### Four methods, each with its own confidence

| Method | Confidence | Example |
|---|---|---|
| `literal` | 1.0 | `AI6US` already in callsign form |
| `phonetic` | 0.9 | `Whiskey Bravo 3 Charlie Sierra Yankee` → WB3CSY |
| `compact` | 0.8 | `KN6, USH` → KN6USH (Whisper split it with punctuation) |
| `roster_fix` | 0.6 | `D9UJK` → KD9UJK (head truncation, repaired from history) |

Every row records `matched_text`, so what produced a given callsign is always visible. A `roster_fix` row keeps the truncated form: `Delta 9 uniform Juliet kilo golf [heard D9UJK]`.

**Measured on 1,754 transcribed transmissions:** 461 mentions across 364 transmissions. **More than 30 distinct callsigns were found that no regex can see**, because they appear only as phonetics or as punctuation-split fragments. On a single net, seven stations existed only through phonetic resolution.

### The dominant failure is head truncation

The first phonetic word of a callsign goes missing far more often than any other. `KD9UJK` arrives as `D9UJK`, `KA8EMH` as `A8EMH`, `KG7GDB` as `G7GDB`. Even net control's own `NN6H` arrived as `N6H` three times in one net.

The likely cause is PTT/link key-up clipping the start of the transmission — an artifact of the medium, not of the software. Two rules address the output:

**Prefix validity.** Only nine single-character prefixes exist worldwide (B, F, G, I, K, M, N, R, W); everything else needs two. Truncations land on impossible prefixes, so this removes the whole class without touching a real callsign. Works with no roster and no history.

**Roster repair.** A parsed callsign that is a proper suffix of exactly one previously-heard callsign is repaired to it. Fires only on a unique match. Off by default (`ENABLE_ROSTER_MATCH=0`).

The repair pass deliberately does *not* skip candidates already in the roster: a truncation that recurs often enough becomes a roster entry in its own right (`N6H` earned three sightings as a clipped `NN6H`), and skipping those would let the commonest truncations immunise themselves from repair.

**The cost of that choice:** if two genuinely different stations on your node have callsigns differing only by a leading character — `N6DT` and `KN6DT`, say — this merges the shorter into the longer. That is why repairs are recorded at 0.6 with the heard form preserved, and why `callsign_roster` has `validated` and `note` columns. **This is the one pass that can be confidently wrong. Review it rather than trusting it.**

### The phonetic map lives in the database

Upstream already ships a `corrections` table holding a hand-maintained phonetic map — 54 rows including the older US military alphabet (`king` → K, `box` → B, `charles` → C), which old-timers still use on the air, and Whisper manglings collected in the field (`kodak` and `kordak` → Q). It appears to be read only by `callsign_extractor.py`, which is not wired into this fork.

The resolver merges that table over its own built-ins, database entries winning. A newly observed mangling is then a one-line `INSERT` rather than a code change, and `--show-map` prints what is actually in effect.

Merging both tables gained 15 forms not in either alone, and produced **no new false positives** across a full net — worth checking, since Hunter's list includes ordinary English words like `king`, `box` and `fox`.

### Honest limits

- **Some truncations survive.** `G7G` and `KG7G` are both clipped `KG7GDB`, but `G` is a legitimate UK prefix and neither string is a unique suffix of the full call. Same for `I60C` — `I` is Italy. These persist as low-confidence `compact` finds.
- **Variants are not merged.** `KC7ZZ`, `KC7Z` and `KC7ZZY` are one operator. Nothing automatic distinguishes that from three stations. This needs a human and the `note` column.
- **`C4FM` looks exactly like a callsign.** "I got my C4 FM radio" rejoins into a valid shape. There is a `NOT_CALLSIGN` list for digital mode names; expect to extend it.
- **Sighting counts need history.** With `ROSTER_MIN_SIGHTINGS=3`, a truncation is only repairable once the full callsign has been heard clearly three times. A single net will not do it.

### Operational notes

- `--rescan-all` runs two passes internally: the first rebuilds the roster, the second applies repairs using it. It clears the roster as well as the mentions, because junk from a previous ruleset would otherwise survive and seed a bad repair.
- `--self-test` runs twelve cases with no database at all. `--test "TEXT"` parses one string. `--version` reports which build is installed.
- Runs as `allstar-callsign-resolver.timer`, every five minutes, scanning only rows it has not seen. Typical run: 370 ms.
- Gated by `ENABLE_CALLSIGN_RESOLVER` in `services.env`. Drop the three tables and the whole feature is gone.

---

## Still open

- `start_recording()` has no maximum duration. If a `txkeyed value: 0` event is missed, the channel stays in `active_recordings` indefinitely — MixMonitor keeps writing one growing file and every subsequent transmission on that channel is silently dropped. A duration cap would bound the damage.
- `extract_ncs()` requires a "this is *callsign* … net control" construction. Neither repeater tested actually talks that way, so `ncs_callsign` has been NULL on every net detected.
- `detect_topics()` includes the bare word `"for"` in its Swap keyword list, so nearly every transcript is tagged swap-and-shop. Harmless while nothing reads `topic_labels`.
- Net naming needs a node-and-schedule lookup rather than speech extraction.
- The AI refinement path is untested here. `_compact_rows()` in `ai_backend.py` filters out transmissions lacking a keyword or regex-matchable callsign before the model sees them, discarding the phonetic spellouts a model handles best. `should_call_ai()` only fires when the regex failed to produce a *name*, so a well-scoring net never reaches the model at all.
- Nothing yet feeds `callsign_mentions` back into `Transcript_Analyzer.py`. The analyzer still counts callsigns with its own regex, so the ≥12-callsign scoring bonus does not benefit from phonetic resolution.
- `callsign_roster.validated` and `.note` are never written. Merging variants of one operator, and marking a resolved callsign as confirmed, are both manual and unsupported by any UI.
- Upstream's `callsigns` table has a broken counter: `Transcript_Analyzer.py` does a bare `INSERT` with no `ON DUPLICATE KEY UPDATE`, so `seen_count` is stuck at 1 for every row. The counting logic exists in `callsign_extractor.py`, which is not part of this fork's service set.
