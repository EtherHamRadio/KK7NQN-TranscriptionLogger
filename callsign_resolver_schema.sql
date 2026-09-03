-- callsign_resolver_schema.sql
--
-- Additive schema for the callsign resolver. Creates three new tables and
-- modifies NOTHING in Hunter Inman's (KK7NQN) original schema. If you drop
-- these three tables the system reverts exactly to stock behaviour.
--
-- Copyright (c) 2026 Tom Salzer (KJ7T). MIT licensed.
--
-- Apply with:
--   mysql -u root -p repeater < callsign_resolver_schema.sql

-- ---------------------------------------------------------------------------
-- callsign_mentions: one row per (transmission, callsign) found.
--
-- method:
--   literal        a token already in callsign form, e.g. "M9SAY"
--   phonetic       assembled from the phonetic alphabet, e.g. whiskey bravo 3...
--   roster_suffix  a fragment matched against previously-seen callsigns
--
-- confidence is advisory, not a probability. Treat < 0.9 as "needs a human".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `callsign_mentions` (
  `id`               int(11)      NOT NULL AUTO_INCREMENT,
  `transcription_id` int(11)      NOT NULL,
  `callsign`         varchar(16)  NOT NULL,
  `method`           varchar(16)  NOT NULL,
  `confidence`       float        NOT NULL DEFAULT 0,
  `matched_text`     varchar(255) DEFAULT NULL,
  `timestamp`        datetime     DEFAULT NULL,
  `created_at`       timestamp    NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_mention` (`transcription_id`, `callsign`, `method`),
  KEY `idx_mention_callsign` (`callsign`),
  KEY `idx_mention_timestamp` (`timestamp`),
  KEY `idx_mention_confidence` (`confidence`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- ---------------------------------------------------------------------------
-- callsign_roster: every callsign this node has confidently heard, with a
-- count. This is the lookup the fuzzy suffix pass consults. It is built only
-- from high-confidence finds, so it does not poison itself with its own
-- guesses.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `callsign_roster` (
  `callsign`   varchar(16) NOT NULL,
  `sightings`  int(11)     NOT NULL DEFAULT 0,
  `first_seen` datetime    DEFAULT NULL,
  `last_seen`  datetime    DEFAULT NULL,
  `note`       varchar(255) DEFAULT NULL,
  PRIMARY KEY (`callsign`),
  KEY `idx_roster_sightings` (`sightings`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- ---------------------------------------------------------------------------
-- callsign_scan_log: progress marker. One row per transcription examined,
-- whether or not anything was found.
--
-- Without this, a transmission containing no callsigns would have no row in
-- callsign_mentions and would be rescanned on every pass forever. This is the
-- same class of bug as re-analysing already-analysed rows -- cheap to avoid,
-- annoying to diagnose later.
--
-- To force a full rescan:  TRUNCATE callsign_scan_log; TRUNCATE callsign_mentions;
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `callsign_scan_log` (
  `transcription_id` int(11)   NOT NULL,
  `found`            tinyint(4) NOT NULL DEFAULT 0,
  `scanned_at`       timestamp NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`transcription_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
