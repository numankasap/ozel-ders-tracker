-- Türk Futbolu Analiz Platformu - Supabase Şeması
-- Süper Lig, TFF 1. Lig, 2. Lig, 3. Lig + Genç Ligler (U19, U21)

-- Ülkeler tablosu
CREATE TABLE countries (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    code VARCHAR(3),
    flag_url TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Ligler tablosu
CREATE TABLE leagues (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    country_id INTEGER REFERENCES countries(id),
    tier INTEGER,  -- 1: Süper Lig, 2: 1.Lig, 3: 2.Lig, 4: 3.Lig, 5: U21, 6: U19
    api_football_id INTEGER,
    logo_url TEXT,
    is_youth BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Sezonlar tablosu
CREATE TABLE seasons (
    id SERIAL PRIMARY KEY,
    year VARCHAR(10) NOT NULL,  -- "2024-2025"
    start_date DATE,
    end_date DATE,
    is_current BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Takımlar tablosu
CREATE TABLE teams (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    short_name VARCHAR(20),
    league_id INTEGER REFERENCES leagues(id),
    api_football_id INTEGER,
    transfermarkt_id VARCHAR(50),
    logo_url TEXT,
    founded_year INTEGER,
    stadium VARCHAR(100),
    city VARCHAR(50),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Oyuncular tablosu
CREATE TABLE players (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    full_name VARCHAR(150),
    birth_date DATE,
    age INTEGER,
    nationality_id INTEGER REFERENCES countries(id),
    second_nationality_id INTEGER REFERENCES countries(id),
    height_cm SMALLINT,
    weight_kg SMALLINT,
    primary_position VARCHAR(30),
    secondary_positions TEXT[],  -- PostgreSQL array
    preferred_foot VARCHAR(10),  -- Left, Right, Both
    api_football_id INTEGER,
    transfermarkt_id VARCHAR(50),
    photo_url TEXT,
    is_youth_player BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Oyuncu-Takım ilişkisi (transfer geçmişi)
CREATE TABLE player_teams (
    id SERIAL PRIMARY KEY,
    player_id INTEGER REFERENCES players(id),
    team_id INTEGER REFERENCES teams(id),
    season_id INTEGER REFERENCES seasons(id),
    jersey_number SMALLINT,
    join_date DATE,
    leave_date DATE,
    transfer_type VARCHAR(30),  -- Transfer, Loan, Free, Academy
    transfer_fee DECIMAL(15,2),
    is_current BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(player_id, team_id, season_id)
);

-- Piyasa değeri geçmişi
CREATE TABLE player_market_values (
    id SERIAL PRIMARY KEY,
    player_id INTEGER REFERENCES players(id),
    recorded_at DATE NOT NULL,
    market_value DECIMAL(15,2),  -- Euro cinsinden
    source VARCHAR(50) DEFAULT 'transfermarkt',
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(player_id, recorded_at, source)
);

-- Sezonluk istatistikler
CREATE TABLE player_season_stats (
    id SERIAL PRIMARY KEY,
    player_id INTEGER REFERENCES players(id),
    team_id INTEGER REFERENCES teams(id),
    season_id INTEGER REFERENCES seasons(id),
    league_id INTEGER REFERENCES leagues(id),
    appearances INTEGER DEFAULT 0,
    starts INTEGER DEFAULT 0,
    minutes_played INTEGER DEFAULT 0,
    goals INTEGER DEFAULT 0,
    assists INTEGER DEFAULT 0,
    yellow_cards INTEGER DEFAULT 0,
    red_cards INTEGER DEFAULT 0,
    -- İleri düzey metrikler
    xG DECIMAL(6,3),
    xA DECIMAL(6,3),
    shots INTEGER DEFAULT 0,
    shots_on_target INTEGER DEFAULT 0,
    pass_accuracy DECIMAL(5,2),
    key_passes INTEGER DEFAULT 0,
    dribbles_completed INTEGER DEFAULT 0,
    tackles_won INTEGER DEFAULT 0,
    interceptions INTEGER DEFAULT 0,
    aerial_duels_won INTEGER DEFAULT 0,
    -- Kaleci istatistikleri
    clean_sheets INTEGER DEFAULT 0,
    saves INTEGER DEFAULT 0,
    goals_conceded INTEGER DEFAULT 0,
    -- Rating
    average_rating DECIMAL(4,2),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(player_id, team_id, season_id, league_id)
);

-- Maç bazlı performans (zaman serisi)
CREATE TABLE match_performances (
    id SERIAL PRIMARY KEY,
    player_id INTEGER REFERENCES players(id),
    team_id INTEGER REFERENCES teams(id),
    match_date DATE NOT NULL,
    opponent_team_id INTEGER REFERENCES teams(id),
    is_home BOOLEAN,
    minutes_played SMALLINT DEFAULT 0,
    goals SMALLINT DEFAULT 0,
    assists SMALLINT DEFAULT 0,
    shots SMALLINT DEFAULT 0,
    shots_on_target SMALLINT DEFAULT 0,
    passes SMALLINT DEFAULT 0,
    pass_accuracy DECIMAL(5,2),
    key_passes SMALLINT DEFAULT 0,
    dribbles_attempted SMALLINT DEFAULT 0,
    dribbles_completed SMALLINT DEFAULT 0,
    tackles SMALLINT DEFAULT 0,
    interceptions SMALLINT DEFAULT 0,
    fouls_committed SMALLINT DEFAULT 0,
    fouls_drawn SMALLINT DEFAULT 0,
    yellow_card BOOLEAN DEFAULT FALSE,
    red_card BOOLEAN DEFAULT FALSE,
    rating DECIMAL(4,2),
    xG DECIMAL(5,3),
    xA DECIMAL(5,3),
    api_fixture_id INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Genç yetenek potansiyel skoru
CREATE TABLE youth_potential_scores (
    id SERIAL PRIMARY KEY,
    player_id INTEGER REFERENCES players(id),
    calculated_at DATE NOT NULL,
    potential_score DECIMAL(5,2),  -- 0-100 arası
    current_ability DECIMAL(5,2),
    physical_score DECIMAL(5,2),
    technical_score DECIMAL(5,2),
    mental_score DECIMAL(5,2),
    predicted_peak_value DECIMAL(15,2),
    predicted_peak_age SMALLINT,
    confidence_level DECIMAL(4,2),  -- Model güven skoru
    model_version VARCHAR(20),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(player_id, calculated_at)
);

-- Scraping log tablosu
CREATE TABLE scraping_logs (
    id SERIAL PRIMARY KEY,
    source VARCHAR(50) NOT NULL,  -- api_football, transfermarkt, fbref
    entity_type VARCHAR(30),  -- player, team, match, stats
    records_fetched INTEGER DEFAULT 0,
    records_inserted INTEGER DEFAULT 0,
    records_updated INTEGER DEFAULT 0,
    errors TEXT,
    started_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP,
    status VARCHAR(20) DEFAULT 'running'  -- running, completed, failed
);

-- İndeksler
CREATE INDEX idx_players_nationality ON players(nationality_id);
CREATE INDEX idx_players_position ON players(primary_position);
CREATE INDEX idx_players_youth ON players(is_youth_player);
CREATE INDEX idx_players_age ON players(age);
CREATE INDEX idx_player_teams_current ON player_teams(is_current);
CREATE INDEX idx_player_teams_player ON player_teams(player_id);
CREATE INDEX idx_player_season_stats_player ON player_season_stats(player_id);
CREATE INDEX idx_player_season_stats_season ON player_season_stats(season_id);
CREATE INDEX idx_match_performances_player ON match_performances(player_id);
CREATE INDEX idx_match_performances_date ON match_performances(match_date);
CREATE INDEX idx_market_values_player ON player_market_values(player_id);
CREATE INDEX idx_market_values_date ON player_market_values(recorded_at);
CREATE INDEX idx_youth_potential_player ON youth_potential_scores(player_id);

-- Row Level Security (RLS)
ALTER TABLE players ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_season_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE match_performances ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_market_values ENABLE ROW LEVEL SECURITY;

-- Public read access
CREATE POLICY "Public read access" ON players FOR SELECT USING (true);
CREATE POLICY "Public read access" ON player_season_stats FOR SELECT USING (true);
CREATE POLICY "Public read access" ON match_performances FOR SELECT USING (true);
CREATE POLICY "Public read access" ON player_market_values FOR SELECT USING (true);

-- Başlangıç verileri: Türkiye ligleri
INSERT INTO countries (name, code) VALUES ('Türkiye', 'TR');

INSERT INTO leagues (name, country_id, tier, api_football_id, is_youth) VALUES
('Süper Lig', 1, 1, 203, FALSE),
('TFF 1. Lig', 1, 2, 204, FALSE),
('TFF 2. Lig', 1, 3, 205, FALSE),
('TFF 3. Lig', 1, 4, 206, FALSE),
('Türkiye U21 Ligi', 1, 5, NULL, TRUE),
('Türkiye U19 Ligi', 1, 6, NULL, TRUE),
('Türkiye Kupası', 1, 0, 207, FALSE);

INSERT INTO seasons (year, is_current) VALUES
('2024-2025', TRUE),
('2023-2024', FALSE),
('2022-2023', FALSE);
