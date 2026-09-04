# AllStar Net Transcriber (KJ7T fork)

Capture, transcribe and log amateur radio net traffic from an AllStarLink hub node.

This is a fork of [KK7NQN-TranscriptionLogger](https://github.com/Wintergrasped/KK7NQN-TranscriptionLogger) by Hunter Inman (KK7NQN), with several bug fixes, a plain-text transcript viewer, a deterministic callsign resolver, and the capture and transcription components the upstream project references but does not include.

Tested in production on AllStarLink hub node 588416 against live nets on four repeaters.

---

## Not for life-safety use

**Do not use this as a system of record for emergency communications.** Not for fire service, EMS, hospitals, an EOC, formal ARES or RACES traffic, or SKYWARN. If a served agency needs a log of what was said, that log needs a person.

This is not boilerplate. It is what was measured on the node this was built on:

- **A bug silently discarded 16% of all captured audio for days.** 2.3 hours of net traffic. It produced no error, no warning, and nothing in any log saying anything was wrong. It was found by accident during unrelated housekeeping.
- **Callsigns are best-effort.** The first phonetic word of a callsign is routinely clipped by PTT key-up — net control's own callsign arrived truncated three times in a single net. The resolver repairs some of these and records a confidence, but it can also be confidently wrong.
- **Two spellings of one station can both appear in the same transcript** with nothing marking the conflict. WB3CSY from the operator's own phonetic ID; KB3CSY from net control's read-back a minute later.
- **Whisper invents words on silence and noise.** A clip with nothing intelligible in it transcribed as "You". Primed with a callsign vocabulary, the same class of clip produced a plausible-looking callsign that nobody had spoken.
- **It is asynchronous, not live.** Transcripts appear seconds to minutes after a transmission ends, and the architecture has a floor of one transmission behind. It cannot provide real-time situational awareness.
- **There is no redundancy and no alerting.** One node, one disk, no monitoring. If it stops, nothing tells you.

Treat a transcript as a searchable aid to memory, not as evidence of what was said. Where accuracy matters, go listen to the audio. Where it matters more than that, have a human keeping the log.

---

## What it does

```
PTT-keyed audio  →  Whisper transcription  →  MariaDB  →  regex net detection  →  web viewer
                                                       →  phonetic callsign resolution
```

1. **`ami_ptt_recorder.py`** listens to Asterisk's AMI and records each transmission as its own WAV via MixMonitor.
2. **`transcribe_watcher.py`** picks up finished WAVs and hands each to the transcriber.
3. **`transcribe_and_log.py`** runs faster-whisper and writes the text to MariaDB.
4. **`Transcript_Analyzer.py`** groups transcripts into sessions, scores each one for net-like language, and writes a `net_data` record when it finds one.
5. **`callsign_resolver.py`** converts spoken phonetics into callsigns and records them with a confidence and provenance.
6. **`netviewer.py`** serves a web page for querying transcripts by time range, exporting them as plain text, and naming detected nets.

Steps 1–3 and 6 run continuously as systemd services. Steps 4 and 5 run on five-minute timers and are independently optional.

---

## Three ways to run this

The pieces are independent services, so you can run only the parts you need.

**Net logging** — the original purpose. Run everything. Expect to tune the phrase lists in `Transcript_Analyzer.py` to match how the nets you monitor actually talk; see *Tuning detection* below. This is the one configuration that requires calibration.

**Repeater archive** — you own or help run a repeater and want a searchable record of traffic. Run the recorder, watcher, transcriber and viewer, and **never enable `allstar-transcript-analyzer.timer`**. You get a time-range-searchable transcript archive with no scoring, no net records, and nothing to tune. The callsign resolver is worth running in this configuration — it makes the archive searchable by station without any net detection at all.

**Accessibility** — following a net you can't hear well, or catching up on one you missed. Same configuration as the archive case. Transcription quality is what matters here, and disabling the VAD filter (see below) makes a substantial difference on marginal RF audio.

> **This is an asynchronous service, not live captioning.** Transcripts appear seconds to minutes after a transmission ends. The architecture has a floor of one transmission — a WAV must be closed before it can be transcribed — so even adding auto-refresh to the viewer would not get you word-by-word captioning during someone's over. That would need a different capture approach entirely.

---

## Requirements

- An AllStarLink node running Asterisk, with AMI enabled and `app_mixmonitor.so` loaded
- Python 3.9+
- MariaDB or MySQL
- `faster-whisper`, `flask`, `mysql-connector-python` in a virtualenv
- Optional: `psutil`, for the analyzer's CPU-throttle check

**Bind AMI to localhost.** Check `manager.conf` for `bindaddr = 0.0.0.0` before you go any further. During this project's build that port was found open to the internet and being actively scanned.

---

## Setup

1. Import the database schema from the upstream repo (`Database/MainDatabase.sql`) and create a database user.
2. Copy `env.example` to `ami.env` and `netviewer.env`, fill in your values, and `chmod 600` both.
3. Create the recording directories, owned by the user Asterisk runs as:

```bash
sudo mkdir -p /opt/allstar-transcriber/recordings/{staging,incoming,processed,failed}
sudo chown -R asterisk:asterisk /opt/allstar-transcriber/recordings
sudo chmod 2775 /opt/allstar-transcriber/recordings/*
```

4. Install the systemd units from `systemd/`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now allstar-ptt-recorder allstar-transcribe-watcher allstar-netviewer
sudo systemctl enable --now allstar-transcript-analyzer.timer   # net logging only
```

**Set `PYTHONUNBUFFERED=1` in every unit.** Without it Python block-buffers stdout when systemd captures it, and every timestamp in your journal is a buffer-flush time rather than an event time. This is not cosmetic — during development it misdirected a bug hunt for an hour by making complete files look as though they had been rejected twenty-five minutes after they finished writing.

### Optional: the callsign resolver

Additive and entirely opt-in. It creates three new tables and changes nothing upstream.

```bash
sudo mysql <your-database> < callsign_resolver_schema.sql
cp services.env.example /opt/allstar-transcriber/services.env
sudo systemctl enable --now allstar-callsign-resolver.timer
```

Then populate from your existing history:

```bash
/opt/allstar-transcriber/venv/bin/python3 callsign_resolver.py --rescan-all
```

To remove it: disable the timer and drop `callsign_mentions`, `callsign_roster` and `callsign_scan_log`.

---

## Tuning detection

`Transcript_Analyzer.py` scores each session and logs a net when the total reaches `NET_SCORE_THRESHOLD` (default 1.0).

| Signal | Weight |
|---|---|
| Strong phrase (`welcome to ... net`, `net control`, `any check-ins`, …) | 0.6 |
| Weak phrase (`roll call`, `check-in(s)`, `in and out`, `for the log`, …) | 0.3 |
| 12 or more unique callsigns in the session | +0.2 |
| Session duration between 20 and 120 minutes | +0.2 |
| Session starts on the hour or half hour | +0.1 |

Each phrase contributes its weight **once**, however many times it occurs. The true count is still recorded in `keyword_hits` for diagnostics. Without that cap a chatty net control saying "in and out" a dozen times scores 3.6 on that phrase alone, and any conversation containing common net vocabulary a few times would be logged as a net.

**Expect to adjust the phrase lists for your repeaters.** Across five live captures, one net produced only a single phrase hit in 97 minutes; another cleared the threshold on weak phrases alone. The three phrases added in this fork were chosen from operator knowledge of specific nets and validated afterward — two of the three contributed nothing on the first net that tested them.

**Set the threshold to match your error preference.** A missed net is effectively permanent: once a session is scored `is_net=0`, the fetch query never revisits it. So if you are logging nets, bias low. If you are archiving general traffic, bias high or leave the analyzer off entirely.

**Session naming is unreliable.** `extract_net_name()` derives names from speech, and speech recognition mangles them — real output from this fork includes "In The Technical Discussions During" and "What Do You Have". Use the rename field in the viewer. A node-and-schedule lookup table would be the proper fix and is not yet built.

---

## Notes on transcription quality

`transcribe_and_log.py` runs faster-whisper with **`vad_filter=False`**. This is deliberate. The Silero VAD pre-filter is tuned for clean audio and discards real speech on weak, noisy RF links — on this node it was blanking about a third of all transmissions, including several that were five to seven seconds long. Whisper's own `no_speech_threshold`, `log_prob_threshold` and `compression_ratio_threshold` remain active as the defence against transcribing noise.

### A bigger model does not fix callsigns

Callsigns are the weak point, so the obvious move is a larger Whisper model. It was benchmarked on real captured audio and rejected, along with two other ways of making the transcriber smarter. All three made callsigns **worse**:

| Attempt | Result |
|---|---|
| `medium` instead of `small` | 2.9× slower; rendered "foxtrot zero" as **"Flaxstrad 0"** |
| `initial_prompt` seeded with phonetics and callsigns | Emitted **"KJ7T-OR."** — straight out of the primer — on unintelligible audio |
| `hotwords` with a callsign list | Produced **"KiloFoxTrad0"** and **"MIK9 SIERRA ALFAYANKI"** |

A larger language model has a stronger prior about what English sounds like, and the phonetic alphabet is deliberately built from words that do not sound like ordinary English. The prior fights the vocabulary. Priming is worse still: a model told to expect callsigns will produce one when it hears nothing at all.

So this fork stays on `small` with `int8`, and resolves callsigns afterward with a lookup table. `small`'s literal "Whiskey Bravo 3, Charlie, Sierra Yankee" looks like a failure and is the most useful output available, because it converts deterministically.

Benchmark on your own audio before switching models — the result may differ on cleaner RF.

---

## Callsign resolution

`callsign_resolver.py` runs after transcription and records each callsign it finds with a method, a confidence, and the text that produced it.

| Method | Confidence | Example |
|---|---|---|
| `literal` | 1.0 | `AI6US` already in callsign form |
| `phonetic` | 0.9 | `Whiskey Bravo 3 Charlie Sierra Yankee` → WB3CSY |
| `compact` | 0.8 | `KN6, USH` → KN6USH |
| `roster_fix` | 0.6 | `D9UJK` → KD9UJK, repaired from previously-heard calls |

On 1,754 transmissions it found 461 mentions, **including more than 30 callsigns that no regex can see** because they appear only as spoken phonetics.

The phonetic map is read from upstream's `corrections` table and merged over the script's built-ins, so a newly observed mangling is one `INSERT` rather than a code change. `--show-map` prints what is in effect.

**The commonest error is head truncation** — the first phonetic word is clipped, probably by PTT key-up. `KD9UJK` arrives as `D9UJK`. Two rules handle it: a prefix-validity check (only B, F, G, I, K, M, N, R and W exist as single-character prefixes worldwide, so truncations land on impossible prefixes), and an optional roster repair that completes a truncation from previously-heard callsigns.

**Roster repair is off by default and is the one pass that can be confidently wrong.** If two real stations differ only by a leading character, it will merge them. Repairs are recorded at 0.6 with the heard form preserved in `matched_text`. Review them.

Treat resolved callsigns as good evidence, not as authoritative. See `CHANGES.md` for the full list of known limits.

---

## Viewer

`netviewer.py` serves on port 8088, bound to all interfaces — reach it over a VPN, not the open internet. It has no authentication, and one write path (renaming a detected net).

- Query transcripts by start and end time
- **Download .txt** — plain text, one transmission per line, with a header giving the window and any detected net. Formatted for pasting into an LLM rather than for a spreadsheet.
- Rename a detected net inline, updating both `net_data` and `transcription_analysis`

---

## AI assistance

The upstream project includes an optional AI refinement path (`ai_backend.py`, defaulting to a local vLLM server, with OpenAI as an alternative). It is **disabled by default and untested in this fork.**

The workflow used here instead is manual: export a transcript with the viewer's Download button and paste it into whatever assistant you already use. This handles the things regex cannot — summarising, identifying net control — while keeping a human in the loop and requiring no API key, no cost, and no external dependency in the pipeline.

Two findings from doing that in anger, both worth knowing before you wire a model into anything:

- **A model with a full context window does not tell you it only read part of your input.** A local model on a small default context silently discarded the beginning of a transcript and produced a fluent, confident, entirely accurate summary of the wrong net.
- **Assistants reconstruct callsigns plausibly rather than correctly, and rarely flag the difference.** In one comparison the model produced `KB3CSY` where the operator's own phonetic ID gave `WB3CSY`. Both spellings were in the transcript; nothing marked the conflict. This is what the resolver's confidence and `matched_text` columns exist to make visible.

If you do enable the built-in path, note that `_compact_rows()` in `ai_backend.py` discards any transmission without a keyword or a regex-matchable callsign before the model sees it, which throws away exactly the phonetic spellouts a model is best at decoding.

---

## Licensing

This project is a fork of [KK7NQN-TranscriptionLogger](https://github.com/Wintergrasped/KK7NQN-TranscriptionLogger) by Hunter Inman (KK7NQN), distributed under the **GPLv3** as required by the upstream license. Modified files carry notices describing what changed and when.

Four components are original work by Tom Salzer (KJ7T) and are additionally available under the **MIT license**: `ami_ptt_recorder.py`, `transcribe_and_log.py`, `netviewer.py`, and `callsign_resolver.py`. They do not import upstream code and can be reused independently. See `LICENSE-MIT`.

See `CHANGES.md` for the full list of modifications and the reasoning behind each.

---

## Credits

Built on Hunter Inman's (KK7NQN) work, presented at the inaugural Zero Retries Digital Conference in Everett, Washington, September 2025. The session-scoring approach, database schema, and analyzer are his. The `corrections` phonetic map the callsign resolver depends on is also his.

Fixes and additions by Tom Salzer, KJ7T — [EtherHam](https://etherham.com).
