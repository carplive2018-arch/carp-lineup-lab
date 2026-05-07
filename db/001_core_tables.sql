CREATE TABLE IF NOT EXISTS players (
    id BIGSERIAL PRIMARY KEY,
    player_code TEXT,
    player_name TEXT NOT NULL,
    bats TEXT,
    throws TEXT,
    team_code TEXT DEFAULT 'C',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_players_team_name UNIQUE (team_code, player_name)
);

CREATE TABLE IF NOT EXISTS games (
    id BIGSERIAL PRIMARY KEY,
    game_date DATE NOT NULL,
    team_code TEXT NOT NULL DEFAULT 'C',
    squad_level TEXT NOT NULL,
    opponent_team_code TEXT,
    opponent_team_name TEXT,
    source_url TEXT,
    source_site TEXT,
    venue TEXT,
    home_away TEXT,
    game_status TEXT DEFAULT 'final',
    runs_for INTEGER DEFAULT 0,
    runs_against INTEGER DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_games_unique UNIQUE (game_date, squad_level, opponent_team_name, source_url)
);

CREATE TABLE IF NOT EXISTS game_lineups (
    id BIGSERIAL PRIMARY KEY,
    game_id BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    player_id BIGINT NOT NULL REFERENCES players(id),
    batting_order INTEGER,
    position TEXT,
    starter_flag BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_game_lineups_unique UNIQUE (game_id, player_id)
);

CREATE TABLE IF NOT EXISTS player_game_batting_stats (
    id BIGSERIAL PRIMARY KEY,
    game_id BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    player_id BIGINT NOT NULL REFERENCES players(id),
    squad_level TEXT NOT NULL,
    batting_order INTEGER,
    position TEXT,
    pa INTEGER NOT NULL DEFAULT 0,
    ab INTEGER NOT NULL DEFAULT 0,
    runs INTEGER NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    doubles INTEGER NOT NULL DEFAULT 0,
    triples INTEGER NOT NULL DEFAULT 0,
    home_runs INTEGER NOT NULL DEFAULT 0,
    rbi INTEGER NOT NULL DEFAULT 0,
    walks INTEGER NOT NULL DEFAULT 0,
    intentional_walks INTEGER NOT NULL DEFAULT 0,
    hit_by_pitch INTEGER NOT NULL DEFAULT 0,
    strikeouts INTEGER NOT NULL DEFAULT 0,
    sac_bunts INTEGER NOT NULL DEFAULT 0,
    sac_flies INTEGER NOT NULL DEFAULT 0,
    stolen_bases INTEGER NOT NULL DEFAULT 0,
    caught_stealing INTEGER NOT NULL DEFAULT 0,
    gidp INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_player_game_batting_stats_unique UNIQUE (game_id, player_id)
);

CREATE TABLE IF NOT EXISTS player_game_fielding_stats (
    id BIGSERIAL PRIMARY KEY,
    game_id BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    player_id BIGINT NOT NULL REFERENCES players(id),
    squad_level TEXT NOT NULL,
    position TEXT NOT NULL,
    innings_defended NUMERIC(5,2) NOT NULL DEFAULT 0,
    started_flag BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_player_game_fielding_stats_unique UNIQUE (game_id, player_id, position)
);

CREATE TABLE IF NOT EXISTS player_metric_snapshots (
    id BIGSERIAL PRIMARY KEY,
    target_date DATE NOT NULL,
    player_id BIGINT NOT NULL REFERENCES players(id),
    player_name TEXT NOT NULL,
    model_type TEXT NOT NULL,
    pa NUMERIC(10,4) NOT NULL DEFAULT 0,
    avg NUMERIC(10,6) NOT NULL DEFAULT 0,
    obp NUMERIC(10,6) NOT NULL DEFAULT 0,
    slg NUMERIC(10,6) NOT NULL DEFAULT 0,
    iso NUMERIC(10,6) NOT NULL DEFAULT 0,
    bb_rate NUMERIC(10,6) NOT NULL DEFAULT 0,
    k_rate NUMERIC(10,6) NOT NULL DEFAULT 0,
    woba_lite NUMERIC(10,6) NOT NULL DEFAULT 0,
    speed_score NUMERIC(10,6) NOT NULL DEFAULT 0,
    gidp_avoidance NUMERIC(10,6) NOT NULL DEFAULT 1,
    batting_score NUMERIC(10,6) NOT NULL DEFAULT 0,
    starter_score NUMERIC(10,6) NOT NULL DEFAULT 0,
    defense_score NUMERIC(10,6) NOT NULL DEFAULT 0,
    position_scarcity NUMERIC(10,6) NOT NULL DEFAULT 0,
    recent_playing_time NUMERIC(10,6) NOT NULL DEFAULT 0,
    opponent_handedness_bonus NUMERIC(10,6) NOT NULL DEFAULT 0,
    eligible_positions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    defense_by_position_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_player_metric_snapshots_unique UNIQUE (target_date, player_id, model_type)
);

CREATE TABLE IF NOT EXISTS lineup_predictions (
    id BIGSERIAL PRIMARY KEY,
    target_date DATE NOT NULL,
    model_type TEXT NOT NULL,
    opponent_team_code TEXT,
    opponent_pitcher_hand TEXT,
    expected_runs NUMERIC(10,6) NOT NULL DEFAULT 0,
    confidence NUMERIC(10,6) NOT NULL DEFAULT 0,
    summary TEXT,
    lineup_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_lineup_predictions_unique UNIQUE (target_date, model_type)
);

CREATE TABLE IF NOT EXISTS lineup_prediction_players (
    id BIGSERIAL PRIMARY KEY,
    prediction_id BIGINT NOT NULL REFERENCES lineup_predictions(id) ON DELETE CASCADE,
    batting_order INTEGER NOT NULL,
    player_id BIGINT NOT NULL REFERENCES players(id),
    player_name TEXT NOT NULL,
    position TEXT,
    starter_score NUMERIC(10,6) NOT NULL DEFAULT 0,
    slot_fit_score NUMERIC(10,6) NOT NULL DEFAULT 0,
    reason TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_lineup_prediction_players_unique UNIQUE (prediction_id, batting_order)
);

CREATE INDEX IF NOT EXISTS idx_games_game_date ON games(game_date);
CREATE INDEX IF NOT EXISTS idx_pgbs_game_player ON player_game_batting_stats(game_id, player_id);
CREATE INDEX IF NOT EXISTS idx_pgfs_game_player ON player_game_fielding_stats(game_id, player_id);
