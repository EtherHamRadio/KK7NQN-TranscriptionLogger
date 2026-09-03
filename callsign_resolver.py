#!/usr/bin/env python3
"""
callsign_resolver.py -- turn spoken phonetics in transcripts into callsigns.

Runs as a post-processing pass over the `transcriptions` table. It reads text
that is already in the database and writes findings to its own tables. It never
modifies a transcript, never touches Hunter Inman's (KK7NQN) schema, and is not
in the capture or transcription path. Disable it and nothing else changes.

WHY THIS EXISTS
---------------
Whisper `small` transcribes ham phonetics literally: "Whiskey Bravo 3, Charlie,
Sierra Yankee". That is not a failure -- it is the most useful thing it could
have done, because a lookup table converts it to WB3CSY every time, for free,
reproducibly.

Benchmarking on 2026-09-03 net audio showed every attempt to make Whisper itself
smarter made this worse:

  medium model    "Kilo, Flaxstrad 0, Sierra, Mike Delta"   (invented a word)
  initial_prompt  "KJ7T-OR."   on unintelligible audio      (echoed the primer)
  hotwords        "KiloFoxTrad0 Sierra Mike Delta"          (mangled the phonetics)

A larger language model has a stronger prior about what English sounds like, and
the phonetic alphabet is deliberately made of words that do not sound like
ordinary English. The prior fights the vocabulary. So we leave the acoustics
alone and do the resolution here, where it is deterministic, inspectable, and
wrong in ways you can see.

MODES
-----
  callsign_resolver.py                 scan transcriptions not yet scanned
  callsign_resolver.py --dry-run       scan and report, write nothing
  callsign_resolver.py --rescan-all    clear findings and rescan everything
                                       (clears the roster too -- run it TWICE:
                                        pass 1 rebuilds the roster, pass 2 uses it)
  callsign_resolver.py --rebuild-roster  recount the roster from mentions
  callsign_resolver.py --test "TEXT"   parse one string and report
  callsign_resolver.py --show-map      print the merged phonetic map
  callsign_resolver.py --self-test     run the built-in test cases (no DB)
  callsign_resolver.py --version       print version and what changed

THE PHONETIC MAP LIVES IN THE DATABASE
--------------------------------------
Hunter's `corrections` table already holds a hand-maintained phonetic map. This
script merges it over its own built-ins, so a newly observed mangling is fixed
with one INSERT rather than a code change:

    INSERT INTO corrections (detect, correct) VALUES ('flaxstrad', 'f');

Run --show-map to see what is actually in effect.

ENV
---
  DB_HOST DB_USER DB_PASS DB_NAME       MariaDB connection
  ENABLE_CALLSIGN_RESOLVER   1/0        master switch (default 1)
  ENABLE_ROSTER_MATCH        1/0        head-truncation repair (default 0)
  ROSTER_MIN_SIGHTINGS       int        confident sightings before a callsign
                                        may be used to repair another (default 3)
  RESOLVER_BATCH_LIMIT       int        rows per run (default 2000)

Copyright (c) 2026 Tom Salzer (KJ7T). MIT licensed.
"""
import os
import re
import sys
import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

# --------------------------------------------------------------------------
# Config loading. Reads KEY=VALUE files so manual runs work without sourcing
# anything; systemd's EnvironmentFile does the same job for the service.
# Values already present in the environment always win.
# --------------------------------------------------------------------------
VERSION = "2026-09-03.4"
VERSION_NOTE = (
    "prefix-validity rule; roster head-truncation repair; two-pass rescan "
    "in one command; roster no longer double-counted on rescan"
)

CONF_FILES = [
    "/opt/allstar-transcriber/db.env",
    "/opt/allstar-transcriber/services.env",
]


def load_env_files(paths: Iterable[str]) -> None:
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    os.environ.setdefault(key, val)
        except OSError:
            pass


def env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Phonetic alphabet, with the misspellings Whisper actually produces.
# Entries here were taken from real captures, not from a standards document --
# "keelo" and "alfayanki" are in this table because Whisper emitted them.
# --------------------------------------------------------------------------
BUILTIN_PHONETIC: Dict[str, str] = {
    "alpha": "A", "alfa": "A", "alfah": "A", "alphuh": "A",
    "bravo": "B", "brava": "B",
    "charlie": "C", "charley": "C", "charly": "C",
    "delta": "D",
    "echo": "E", "eco": "E",
    "foxtrot": "F", "fox": "F", "foxtrott": "F", "foxtrat": "F",
    "golf": "G", "gulf": "G",
    "hotel": "H",
    "india": "I", "indigo": "I",
    "juliet": "J", "juliett": "J", "julia": "J", "juliette": "J",
    "kilo": "K", "keelo": "K", "kelo": "K", "keylo": "K",
    "lima": "L", "leema": "L", "limah": "L",
    "mike": "M", "mic": "M", "mik": "M",
    "november": "N", "novembre": "N",
    "oscar": "O", "oskar": "O",
    "papa": "P", "poppa": "P", "pappa": "P",
    "quebec": "Q", "kebec": "Q",
    "romeo": "R", "romero": "R",
    "sierra": "S", "siera": "S",
    "tango": "T",
    "uniform": "U",
    "victor": "V", "viktor": "V",
    "whiskey": "W", "whisky": "W", "wiskey": "W",
    "xray": "X", "x-ray": "X",
    "yankee": "Y", "yanky": "Y", "yankie": "Y",
    "zulu": "Z", "zoolu": "Z",
}

# Digits. Deliberately EXCLUDES the homophones that appear constantly in
# ordinary speech: "for", "to", "too", "won", "ate", "o". Including them turned
# "...Sierra Yankee for the log" into WB3CSY4. The trailing-trim below is a
# second line of defence, but the cheapest fix is not to create the problem.
BUILTIN_DIGITS: Dict[str, str] = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "tree": "3",
    "four": "4", "fower": "4", "five": "5", "fife": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "niner": "9",
    "0": "0", "1": "1", "2": "2", "3": "3", "4": "4",
    "5": "5", "6": "6", "7": "7", "8": "8", "9": "9",
}

# --------------------------------------------------------------------------
# Live maps. These start as copies of the built-ins and are then extended from
# Hunter's `corrections` table, which already holds a phonetic map maintained
# by hand (alpha->a, kodak->q, king->k -- the last being the older US military
# alphabet that old-timers still use on the air).
#
# Putting the table in the database rather than in this file means a new
# mangling can be fixed with one INSERT, by a web form, or by anyone running
# this stack who has never opened a Python file. The built-ins below are the
# fallback for a database that has no `corrections` table at all.
# --------------------------------------------------------------------------
PHONETIC: Dict[str, str] = dict(BUILTIN_PHONETIC)
DIGITS: Dict[str, str] = dict(BUILTIN_DIGITS)

# Multi-word detect keys, e.g. "key local" -> K. Handled by substitution before
# tokenising, since the tokeniser splits on whitespace and would never see them.
MULTIWORD: Dict[str, str] = {}


def load_corrections(cur) -> Tuple[int, int]:
    """
    Merge Hunter's `corrections` table into the live maps.

    Only single-character targets are treated as phonetics; anything else in
    that table is left for whatever else uses it. Database entries win over the
    built-ins, so a correction added by hand always takes effect.

    Returns (new_forms, overridden_forms). A row whose key already exists with
    the same value is neither -- it is a no-op, and counting it as an addition
    makes the log claim the map grew when it did not.

    Missing table is not an error.
    """
    added = overridden = 0
    try:
        cur.execute("SELECT `detect`, `correct` FROM corrections")
        rows = cur.fetchall()
    except Exception:
        return (0, 0)

    for detect, correct in rows:
        if not detect or not correct:
            continue
        key = str(detect).strip().lower()
        val = str(correct).strip()
        if len(val) != 1 or not val.isalnum():
            continue
        target = DIGITS if val.isdigit() else PHONETIC
        if " " in key:
            if key not in MULTIWORD:
                added += 1
            MULTIWORD[key] = val.upper()
            continue
        clean = normalise(key)
        if not clean:
            continue
        new_val = val.upper()
        if clean not in target:
            added += 1
        elif target[clean] != new_val:
            overridden += 1
        target[clean] = new_val
    return (added, overridden)


def apply_multiword(text: str) -> str:
    """Replace multi-word phonetic phrases with a single synthetic token."""
    if not MULTIWORD:
        return text
    for phrase, ch in MULTIWORD.items():
        token = f"phz{ch.lower()}"
        if ch.isdigit():
            DIGITS.setdefault(token, ch)
        else:
            PHONETIC.setdefault(token, ch)
        text = re.sub(re.escape(phrase), token, text, flags=re.I)
    return text


# Callsign shape. Same rule as the (corrected) regex in Transcript_Analyzer.py
# so both components agree on what a callsign is. Q prefixes are reserved and
# excluded. A digit is mandatory, which is what keeps ordinary speech out --
# "Oscar Mike Golf" yields OMG, which has no digit and is rejected.
CALLSIGN_SHAPE = re.compile(r"^[A-PR-Z]{1,2}\d{1,4}[A-Z]{1,3}$")

# Only nine single-character prefixes exist anywhere in the world:
#   B China   F France   G UK   I Italy   K USA
#   M UK      N USA      R Russia         W USA
# Every other prefix is at least two characters. This matters because the
# commonest failure in this data is the FIRST phonetic word going missing --
# "Delta 9 uniform Juliet kilo golf" is KD9UJK with the "kilo" clipped, most
# likely by PTT/link key-up eating the start of the transmission. The
# truncations land on impossible prefixes (D9UJK, A8EMH, O6E), so this rule
# removes a whole class of false positives without touching a real callsign.
VALID_SINGLE_PREFIXES: Set[str] = {"B", "F", "G", "I", "K", "M", "N", "R", "W"}

PREFIX_RE = re.compile(r"^([A-Z]+)")


def plausible_callsign(cs: str) -> bool:
    """Shape is right AND the prefix is one that some country actually issues."""
    if not CALLSIGN_SHAPE.match(cs):
        return False
    m = PREFIX_RE.match(cs)
    if not m:
        return False
    prefix = m.group(1)
    if len(prefix) == 1 and prefix not in VALID_SINGLE_PREFIXES:
        return False
    return True
CALLSIGN_LITERAL = re.compile(
    r"\b([A-PR-Z]{1,2}\d{1,4}[A-Z]{1,3})(?:/[A-Z0-9]+)?\b"
)

# Uppercase fragments that are never callsign tails. Whisper capitalises
# acronyms freely and most of them are radio jargon, not people.
FRAGMENT_STOPLIST: Set[str] = {
    "OK", "AM", "PM", "US", "USA", "UK", "TV", "AI", "RF", "HF", "VHF", "UHF",
    "DMR", "PL", "HT", "QSO", "QSL", "QRM", "QRZ", "AND", "THE", "FOR", "ID",
    "GPS", "USB", "CB", "FM", "SSB", "APRS", "IRLP", "EOC", "ARES", "RACES",
    "NCS", "NET", "TX", "RX", "PTT", "COS", "CTCSS", "DTMF", "IP", "AC", "DC",
    "LED", "USA", "NOAA", "ARRL", "FCC", "SWR", "DX", "CQ", "CW", "OM", "YL",
}

# Alphanumerics that pass the callsign shape test but are not callsigns.
# C4FM is the one that actually bit us -- it is a digital voice mode, it comes
# up constantly on these nets, and "C4 FM" rejoins into a perfectly valid-
# looking callsign.
NOT_CALLSIGN: Set[str] = {
    "C4FM", "P25", "FT8", "FT4", "JS8", "M17", "DV4", "DR1",
    "D74", "D78", "FT2", "FT3", "FT5", "ID52", "ID51",
}

TOKEN_SPLIT = re.compile(r"[\s,;:./\-]+")
TOKEN_CLEAN = re.compile(r"[^a-z0-9]")


def normalise(token: str) -> str:
    """Lowercase and strip punctuation, preserving digits."""
    return TOKEN_CLEAN.sub("", token.lower())


def find_phonetic_callsigns(text: str) -> List[Tuple[str, str]]:
    """
    Find callsigns spelled out phonetically.

    Returns a list of (callsign, matched_text) pairs.

    Walks the text collecting maximal runs of tokens that map to a letter or a
    digit. A run is only considered if it is at least MIN_RUN tokens long. The
    assembled string is then trimmed from the right until it satisfies the
    callsign shape -- this recovers WB3CSY from a run that picked up a trailing
    digit, without ever extending a match.
    """
    MIN_RUN = 3
    raw_tokens = [t for t in TOKEN_SPLIT.split(apply_multiword(text)) if t]
    results: List[Tuple[str, str]] = []

    run_chars: List[str] = []
    run_words: List[str] = []

    def flush() -> None:
        if len(run_chars) < MIN_RUN:
            return
        candidate = "".join(run_chars).upper()
        matched = " ".join(run_words)
        # Trim trailing characters until the shape validates.
        while len(candidate) >= MIN_RUN:
            if CALLSIGN_SHAPE.match(candidate):
                results.append((candidate, matched))
                return
            candidate = candidate[:-1]

    for tok in raw_tokens:
        key = normalise(tok)
        if not key:
            continue
        if key in PHONETIC:
            run_chars.append(PHONETIC[key])
            run_words.append(tok)
        elif key in DIGITS:
            run_chars.append(DIGITS[key])
            run_words.append(tok)
        elif (
            not run_chars
            and len(tok) <= 3
            and tok.isupper()
            and tok.isalnum()
            and any(c.isdigit() for c in tok)
        ):
            # Mixed form: operators often say the prefix as letters and the
            # suffix phonetically -- "K6 Victor Papa". A short uppercase token
            # containing a digit may SEED a run, but never extend one.
            run_chars.extend(tok)
            run_words.append(tok)
        else:
            flush()
            run_chars, run_words = [], []
    flush()

    return results


def find_literal_callsigns(text: str) -> List[Tuple[str, str]]:
    """Find tokens already written as callsigns, e.g. 'M9SAY' or 'NN6H'."""
    out = []
    for m in CALLSIGN_LITERAL.finditer(text.upper()):
        out.append((m.group(1), m.group(0)))
    return out


def find_compact_callsigns(text: str) -> List[Tuple[str, str]]:
    """
    Rejoin callsigns that Whisper split with punctuation.

    Net control says "KN6USH"; Whisper writes "KN6, USH". The literal pass
    misses it because the regex needs contiguous characters. This slides a
    2- and 3-token window over the text, and where every token in the window is
    a short all-uppercase alphanumeric run, tests whether joining them yields a
    valid callsign.

    "Ken, KB7, DFP" -> KB7DFP.  "Mike, KN6, USH" -> KN6USH.

    The all-uppercase requirement is what keeps this safe: "Ten-six, USH" has
    two lowercase tokens and is correctly ignored.
    """
    toks = [t.strip(" ,.;:") for t in TOKEN_SPLIT.split(text) if t.strip(" ,.;:")]
    ok = [
        bool(t) and len(t) <= 4 and t.isupper() and t.isalnum()
        for t in toks
    ]
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for size in (3, 2):
        for i in range(len(toks) - size + 1):
            if not all(ok[i:i + size]):
                continue
            joined = "".join(toks[i:i + size])
            if not CALLSIGN_SHAPE.match(joined) or joined in seen:
                continue
            # Require an actual digit somewhere in the pieces, not just a
            # shape match on letters that happen to line up.
            if not any(ch.isdigit() for ch in joined):
                continue
            seen.add(joined)
            out.append((joined, " ".join(toks[i:i + size])))
    return out


def complete_from_roster(candidate: str, roster: Set[str]) -> Optional[str]:
    """
    Repair a head-truncated callsign using callsigns already heard on this node.

    When the first phonetic word is clipped, what survives is a proper SUFFIX of
    the real callsign: KD9UJK arrives as D9UJK, KA8EMH as A8EMH. If exactly one
    roster entry ends with the candidate, that is almost certainly what was
    said.

    Returns the completed callsign, or None. Only fires on a UNIQUE match --
    an ambiguous suffix yields nothing, because a wrong callsign in the log is
    worse than a missing one.

    Note this is a strictly better-founded use of the roster than matching bare
    fragments out of the text: the candidate here is already a well-formed
    callsign assembled from phonetics, not a guess about a stray token.

    IMPORTANT -- this deliberately does NOT skip candidates that are themselves
    in the roster. A truncation that recurs often enough becomes a roster entry
    in its own right (N6H earned 3 sightings as a clipped NN6H), and skipping
    those would let the commonest truncations immunise themselves from repair.

    The cost of that choice: if two genuinely different stations on your node
    have callsigns differing only by a leading character -- N6DT and KN6DT, say
    -- this merges the shorter into the longer. That is why the result is
    recorded at 0.6 with the heard form kept in matched_text, and why
    callsign_roster has `validated` and `note` columns. This is the one pass
    that can be confidently wrong; review it rather than trusting it.
    """
    hits = [c for c in roster if c.endswith(candidate) and len(c) > len(candidate)]
    return hits[0] if len(hits) == 1 else None


def resolve(
    text: str, roster: Optional[Set[str]] = None
) -> List[Tuple[str, str, float, str]]:
    """
    Full resolution for one transmission.

    Returns (callsign, method, confidence, matched_text), highest confidence
    first, deduplicated by callsign.
    """
    found: Dict[str, Tuple[str, str, float, str]] = {}

    for cs, matched in find_literal_callsigns(text):
        found.setdefault(cs, (cs, "literal", 1.0, matched))

    for cs, matched in find_phonetic_callsigns(text):
        if cs not in found:
            found[cs] = (cs, "phonetic", 0.9, matched)

    for cs, matched in find_compact_callsigns(text):
        if cs not in found:
            found[cs] = (cs, "compact", 0.8, matched)

    # ----------------------------------------------------------------------
    # Repair pass, then validity pass. Order matters: a head-truncated
    # candidate like D9UJK must get its chance to become KD9UJK BEFORE the
    # prefix rule throws it away for having an impossible prefix.
    # ----------------------------------------------------------------------
    if roster:
        for cs in list(found):
            fixed = complete_from_roster(cs, roster)
            if fixed and fixed not in found:
                _, _, conf, matched = found.pop(cs)
                # Keep what was actually heard in matched_text -- the repair
                # must stay visible, never silently rewritten.
                found[fixed] = (
                    fixed, "roster_fix", 0.6,
                    f"{matched} [heard {cs}]"[:255],
                )

    for cs in list(found):
        if not plausible_callsign(cs):
            found.pop(cs)

    # Applied last, so no pass can sneak a known non-callsign back in.
    for junk in NOT_CALLSIGN:
        found.pop(junk, None)

    return sorted(found.values(), key=lambda r: (-r[2], r[0]))


# --------------------------------------------------------------------------
# Self-test. Every case below is real text from node 588416 captures.
# --------------------------------------------------------------------------
SELF_TEST_CASES = [
    ("Whiskey Bravo 3, Charlie, Sierra Yankee for the lock. Thanks, Tom.",
     {"WB3CSY"}),
    ("Kilo foxtrot zero Sierra Mike Delta for the look.", {"KF0SMD"}),
    ("kilo six Victor Papa for the log", {"K6VP"}),
    ("Mike 9, Sierra Alpha Yankee, M9SAY.", {"M9SAY"}),
    ("Okay, Dave. Got you as an in and out. And then Mike, KN6, USH.",
     {"KN6USH"}),
    # Must NOT fire: ordinary speech containing phonetic words but no digit.
    ("Good morning Ned, have a good day, 70 trees, in and out, bye bye.",
     set()),
    ("Hey there Mark, Ryan Hall y'all on YouTube all right very good", set()),
    # Must NOT fire: the trailing-digit trap.
    ("Very good. Delta Airlines was fine and Victor said hello.", set()),
    # Head truncation: "kilo" clipped off KD9UJK. With no roster there is
    # nothing to repair it with, and a bare D prefix exists in no country, so
    # the right answer is to report nothing rather than invent D9UJK.
    ("Delta 9 uniform Juliet kilo golf", set()),
    # Same audio, but this node has heard KD9UJK before.
    ("Delta 9 uniform Juliet kilo golf", {"KD9UJK"}, {"KD9UJK"}),
    # Ambiguous suffix must NOT be repaired: two roster entries end in 6VP.
    ("Delta 9 uniform Juliet", set(), {"KD9UJK", "WD9UJK"}),
    # A real single-letter prefix survives the rule (M is the UK).
    ("Mike 9, Sierra Alpha Yankee", {"M9SAY"}),
]


def self_test() -> int:
    failures = 0
    for case in SELF_TEST_CASES:
        text, expected = case[0], case[1]
        roster = case[2] if len(case) > 2 else None
        got = {r[0] for r in resolve(text, roster)}
        ok = got == expected
        if not ok:
            failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {text[:62]!r}")
        if not ok:
            print(f"        expected {sorted(expected)}, got {sorted(got)}")
    print(f"\n{len(SELF_TEST_CASES) - failures}/{len(SELF_TEST_CASES)} passed")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# Database work
# --------------------------------------------------------------------------
def db_connect():
    import mysql.connector
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        user=os.getenv("DB_USER", "transcriber"),
        password=os.getenv("DB_PASS", ""),
        database=os.getenv("DB_NAME", "repeater"),
    )


def load_roster(cur, min_sightings: int) -> Set[str]:
    cur.execute(
        "SELECT callsign FROM callsign_roster WHERE sightings >= %s",
        (min_sightings,),
    )
    return {row[0] for row in cur.fetchall()}


def say(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    load_env_files(CONF_FILES)
    args = sys.argv[1:]

    if "--version" in args:
        print(f"callsign_resolver.py {VERSION}\n  {VERSION_NOTE}")
        return 0

    if "--self-test" in args:
        return self_test()

    # Best-effort: pull the phonetic map from the database for the offline
    # modes too, so --test and --show-map reflect what a real run would do.
    def try_corrections() -> str:
        try:
            c = db_connect()
            cur_ = c.cursor()
            new, over = load_corrections(cur_)
            cur_.close()
            c.close()
            return (f"corrections table: {new} new forms, {over} overrides "
                    f"({len(PHONETIC)} letter forms, {len(DIGITS)} digit forms)")
        except Exception as e:
            return f"corrections table unavailable ({type(e).__name__}); built-ins only"

    if "--show-map" in args:
        say(try_corrections())
        say(f"\nPHONETIC ({len(PHONETIC)} entries)")
        for ch in sorted(set(PHONETIC.values())):
            words = sorted(k for k, v in PHONETIC.items() if v == ch)
            say(f"  {ch}: {' '.join(words)}")
        say(f"\nDIGITS ({len(DIGITS)} entries)")
        for ch in sorted(set(DIGITS.values())):
            words = sorted(k for k, v in DIGITS.items() if v == ch)
            say(f"  {ch}: {' '.join(words)}")
        if MULTIWORD:
            say(f"\nMULTIWORD: {MULTIWORD}")
        return 0

    if "--test" in args:
        i = args.index("--test")
        if i + 1 >= len(args):
            say("--test needs a string argument")
            return 1
        say(try_corrections())
        for cs, method, conf, matched in resolve(args[i + 1]):
            say(f"  {cs:<10} {method:<14} {conf:.1f}  <- {matched!r}")
        return 0

    if not env_flag("ENABLE_CALLSIGN_RESOLVER", True):
        say("[SKIP] ENABLE_CALLSIGN_RESOLVER is off; nothing to do.")
        return 0

    dry_run = "--dry-run" in args
    rescan_all = "--rescan-all" in args
    rebuild_roster = "--rebuild-roster" in args
    use_roster = env_flag("ENABLE_ROSTER_MATCH", False)
    min_sightings = int(os.getenv("ROSTER_MIN_SIGHTINGS", "3"))
    limit = int(os.getenv("RESOLVER_BATCH_LIMIT", "2000"))

    conn = db_connect()
    cur = conn.cursor()

    new_forms, overrides = load_corrections(cur)
    say(f"[INFO] Phonetic map: {new_forms} new forms and {overrides} overrides "
        f"from `corrections`; {len(PHONETIC)} letter forms, "
        f"{len(DIGITS)} digit forms in effect."
        + ("" if new_forms or overrides else
           "  (Nothing new -- built-ins already covered that table.)"))

    if rebuild_roster:
        say("[INFO] Rebuilding roster from high-confidence mentions...")
        if not dry_run:
            cur.execute("DELETE FROM callsign_roster")
            cur.execute(
                "INSERT INTO callsign_roster "
                "  (callsign, sightings, first_seen, last_seen) "
                "SELECT callsign, COUNT(*), MIN(timestamp), MAX(timestamp) "
                "FROM callsign_mentions WHERE confidence >= 0.9 "
                "GROUP BY callsign"
            )
            conn.commit()
        cur.execute("SELECT COUNT(*) FROM callsign_roster")
        say(f"[OK] Roster holds {cur.fetchone()[0]} callsigns.")
        cur.close()
        conn.close()
        return 0

    def clear(include_roster: bool) -> None:
        if dry_run:
            return
        cur.execute("DELETE FROM callsign_mentions")
        cur.execute("DELETE FROM callsign_scan_log")
        if include_roster:
            cur.execute("DELETE FROM callsign_roster")
        conn.commit()

    if rescan_all:
        say("[WARN] --rescan-all: clearing mentions, scan log AND roster.")
        clear(include_roster=True)

    roster = load_roster(cur, min_sightings) if use_roster else set()
    if use_roster:
        say(f"[INFO] Roster pass enabled: {len(roster)} callsigns "
            f"with >= {min_sightings} sightings.")

    def scan(active_roster: Set[str], label: str = "") -> Tuple[int, int]:
        """Scan every transcription with no scan_log row. Returns (rows, hits)."""
        cur.execute(
            "SELECT t.id, t.transcription, t.timestamp "
            "FROM transcriptions t "
            "LEFT JOIN callsign_scan_log s ON s.transcription_id = t.id "
            "WHERE s.transcription_id IS NULL "
            "  AND t.transcription IS NOT NULL AND t.transcription <> '' "
            "ORDER BY t.id LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
        say(f"[INFO] {label}{len(rows)} transmissions to scan.")
        return _scan_rows(rows, active_roster)

    def _scan_rows(rows, active_roster: Set[str]) -> Tuple[int, int]:
        total_found = 0
        rows_with_finds = 0
        for tid, text, ts in rows:
            hits = resolve(text or "", active_roster or None)
            if hits:
                rows_with_finds += 1
                total_found += len(hits)
            if dry_run:
                if hits:
                    say(f"  #{tid} {ts}")
                    for cs, method, conf, matched in hits:
                        say(f"      {cs:<10} {method:<14} {conf:.1f}"
                            f"  <- {matched!r}")
                continue

            for cs, method, conf, matched in hits:
                cur.execute(
                    "INSERT IGNORE INTO callsign_mentions "
                    "  (transcription_id, callsign, method, confidence, "
                    "   matched_text, timestamp) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (tid, cs, method, conf, matched[:255], ts),
                )
                # Only confident finds feed the roster, so the repair pass can
                # never bootstrap itself off its own guesses.
                if conf >= 0.9:
                    cur.execute(
                        "INSERT INTO callsign_roster "
                        "  (callsign, sightings, first_seen, last_seen) "
                        "VALUES (%s, 1, %s, %s) "
                        "ON DUPLICATE KEY UPDATE "
                        "  sightings = sightings + 1, "
                        "  first_seen = LEAST(COALESCE(first_seen, %s), %s), "
                        "  last_seen  = GREATEST(COALESCE(last_seen, %s), %s)",
                        (cs, ts, ts, ts, ts, ts, ts),
                    )
            cur.execute(
                "INSERT IGNORE INTO callsign_scan_log (transcription_id, found) "
                "VALUES (%s, %s)",
                (tid, 1 if hits else 0),
            )
        if not dry_run:
            conn.commit()
        return rows_with_finds, total_found

    # ----------------------------------------------------------------------
    # A full rescan with repair enabled needs TWO passes, because the repair
    # pass consults a roster that the first pass is still building. Doing it
    # inside one command removes an idiom the user would otherwise have to
    # remember -- and that I got wrong myself the first time, by clearing the
    # roster at the start of every run so a manual second pass could never see
    # what the first one built.
    # ----------------------------------------------------------------------
    two_pass = rescan_all and use_roster and not dry_run

    found_rows, mentions = scan(roster, "pass 1: " if two_pass else "")
    say(f"[{'DRY' if dry_run else 'OK'}] {found_rows} transmissions had "
        f"callsigns; {mentions} mentions.")

    if two_pass:
        roster = load_roster(cur, min_sightings)
        say(f"[INFO] Roster now holds {len(roster)} callsigns with "
            f">= {min_sightings} sightings; re-scanning to apply repairs.")
        # The roster is cleared here TOO, even though pass 2 needs it, because
        # `roster` above is already an in-memory snapshot. Leaving the table in
        # place would let pass 2 increment every sighting a second time -- the
        # counts would silently double, and ROSTER_MIN_SIGHTINGS would quietly
        # mean half what it says.
        clear(include_roster=True)
        found_rows, mentions = scan(roster, "pass 2: ")
        say(f"[OK] {found_rows} transmissions had callsigns; "
            f"{mentions} mentions after repair.")

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
