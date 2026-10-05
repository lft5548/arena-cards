CREATE TABLE IF NOT EXISTS players (
  player_id VARCHAR(64) PRIMARY KEY,
  nickname VARCHAR(64) NOT NULL,
  rating INT NOT NULL DEFAULT 1000,
  wins INT NOT NULL DEFAULT 0,
  losses INT NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS matches (
  match_id VARCHAR(64) PRIMARY KEY,
  player_a VARCHAR(64) NOT NULL,
  player_b VARCHAR(64) NOT NULL,
  winner_id VARCHAR(64) NULL,
  status VARCHAR(16) NOT NULL,
  result_reason VARCHAR(32) NOT NULL DEFAULT '',
  turn_count INT NOT NULL DEFAULT 0,
  seed BIGINT NOT NULL DEFAULT 0,
  started_at TIMESTAMP NULL,
  ended_at TIMESTAMP NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_match_players (match_id),
  CONSTRAINT fk_match_a FOREIGN KEY (player_a) REFERENCES players(player_id),
  CONSTRAINT fk_match_b FOREIGN KEY (player_b) REFERENCES players(player_id)
);

CREATE TABLE IF NOT EXISTS match_results (
  match_id VARCHAR(64) PRIMARY KEY,
  winner_id VARCHAR(64) NULL,
  loser_id VARCHAR(64) NULL,
  winner_rating_delta INT NOT NULL DEFAULT 0,
  loser_rating_delta INT NOT NULL DEFAULT 0,
  settled_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_result_match FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS room_checkpoints (
  match_id VARCHAR(64) PRIMARY KEY,
  checkpoint MEDIUMBLOB NOT NULL,
  owner_id VARCHAR(64) NOT NULL DEFAULT '',
  recovery_format VARCHAR(16) NOT NULL DEFAULT 'full',
  snapshot_sequence BIGINT UNSIGNED NOT NULL DEFAULT 0,
  current_sequence BIGINT UNSIGNED NOT NULL DEFAULT 0,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_room_checkpoint_match FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS room_recovery_tail (
  match_id VARCHAR(64) NOT NULL,
  sequence BIGINT UNSIGNED NOT NULL,
  payload MEDIUMBLOB NOT NULL,
  PRIMARY KEY (match_id, sequence),
  CONSTRAINT fk_room_recovery_tail_checkpoint FOREIGN KEY (match_id)
    REFERENCES room_checkpoints(match_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settlement_outbox (
  match_id VARCHAR(64) PRIMARY KEY,
  winner_id VARCHAR(64) NULL,
  loser_id VARCHAR(64) NULL,
  winner_rating_delta INT NOT NULL DEFAULT 0,
  loser_rating_delta INT NOT NULL DEFAULT 0,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  attempts INT NOT NULL DEFAULT 0,
  last_error VARCHAR(255) NOT NULL DEFAULT '',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  applied_at TIMESTAMP NULL,
  KEY idx_settlement_outbox_status (status, created_at),
  CONSTRAINT fk_outbox_match FOREIGN KEY (match_id) REFERENCES matches(match_id)
);
