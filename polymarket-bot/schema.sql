-- ═══════════════════════════════════════════════════════════════
-- Polymarket Otonom Ticaret Botu - Supabase Veritabani Semasi
-- ═══════════════════════════════════════════════════════════════
-- Bu SQL dosyasini Supabase SQL Editor'de calistirin.
-- Tablolar, ajanin "dis hipokampusu" olarak gorev yapar.
-- ═══════════════════════════════════════════════════════════════

-- ─── 1. bot_state: Ajanin Kimligi ve Konfigurasyonu ─────────
-- Key-Value yapisi ile esnek konfigurasyon.
-- Kill switch, risk parametreleri, bakiye bilgisi burada tutulur.

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Varsayilan konfigurasyonlari ekle
INSERT INTO bot_state (key, value) VALUES
    ('config', '{"is_alive": true}'::jsonb),
    ('risk_params', '{
        "kelly_fraction": 0.25,
        "max_position_pct": 0.20,
        "max_single_order_usdc": 50.0,
        "stop_out_pct": 0.20,
        "min_edge": 0.10,
        "emergency_threshold_pct": 0.20,
        "emergency_min_confidence": 0.90
    }'::jsonb),
    ('balance', '{"usdc": 0.0, "matic": 0.0}'::jsonb),
    ('initial_balance', '0'::jsonb),
    ('market_config', '{
        "min_volume": 10000,
        "min_liquidity": 5000,
        "max_expiry_days": 180,
        "min_expiry_hours": 6,
        "allowed_tags": null,
        "blocked_tags": null
    }'::jsonb)
ON CONFLICT (key) DO NOTHING;


-- ─── 2. positions: Acik Pozisyonlar Envanteri ───────────────
-- Ajanin sahip oldugu tokenlari takip eder.
-- Blokzinciri verileriyle duzenlii olarak senkronize edilir.

CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    size FLOAT NOT NULL DEFAULT 0,
    entry_price FLOAT NOT NULL DEFAULT 0,
    stop_loss FLOAT,
    is_open BOOLEAN NOT NULL DEFAULT TRUE,
    source TEXT DEFAULT 'bot',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,

    PRIMARY KEY (condition_id, token_id)
);

CREATE INDEX IF NOT EXISTS idx_positions_open
    ON positions (is_open) WHERE is_open = TRUE;


-- ─── 3. orders: Emir Tarihcesi ──────────────────────────────
-- Gonderilen her emrin durumu ve detaylari.

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    size FLOAT NOT NULL,
    price FLOAT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'FILLED', 'PARTIALLY_FILLED', 'CANCELLED', 'EXPIRED')),
    filled_size FLOAT DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_orders_status
    ON orders (status) WHERE status = 'OPEN';

CREATE INDEX IF NOT EXISTS idx_orders_market
    ON orders (market_id);


-- ─── 4. market_opportunities: LLM Analiz Onbellegi ──────────
-- Analiz edilen piyasalarin onbellegi.
-- Ayni piyasa 4 saat icinde tekrar analiz edilmez (LLM maliyeti).

CREATE TABLE IF NOT EXISTS market_opportunities (
    condition_id TEXT PRIMARY KEY,
    question TEXT,
    market_price FLOAT,
    ai_probability FLOAT,
    news_summary TEXT,
    trade_action TEXT DEFAULT 'PENDING',
    last_analyzed TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_opportunities_analyzed
    ON market_opportunities (last_analyzed);


-- ─── 5. trade_logs: Islem Gunlugu ve Oz-Yansitma ────────────
-- Her islemin neden yapildigini kaydeder.
-- Retrospektif analiz ve kendini gelistirme icin kritik.

CREATE TABLE IF NOT EXISTS trade_logs (
    id BIGSERIAL PRIMARY KEY,
    order_id TEXT,
    condition_id TEXT,
    question TEXT,
    side TEXT,
    size FLOAT,
    price FLOAT,
    ai_probability FLOAT,
    market_price FLOAT,
    edge FLOAT,
    kelly_fraction FLOAT,
    rationale TEXT,
    risk_level TEXT,
    actual_outcome TEXT,       -- Piyasa kapandiginda guncellenir
    pnl FLOAT,                -- Gerceklesen kar/zarar
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trade_logs_created
    ON trade_logs (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_trade_logs_condition
    ON trade_logs (condition_id);


-- ─── 6. Gorunumler (Views) ──────────────────────────────────

-- Gunluk PnL ozeti
CREATE OR REPLACE VIEW daily_pnl AS
SELECT
    DATE(created_at) AS trade_date,
    COUNT(*) AS trade_count,
    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,
    ROUND(SUM(pnl)::numeric, 4) AS total_pnl,
    ROUND(AVG(edge)::numeric, 4) AS avg_edge,
    ROUND(AVG(ai_probability)::numeric, 4) AS avg_ai_prob
FROM trade_logs
WHERE pnl IS NOT NULL
GROUP BY DATE(created_at)
ORDER BY trade_date DESC;


-- Performans ozeti (genel)
CREATE OR REPLACE VIEW performance_summary AS
SELECT
    COUNT(*) AS total_trades,
    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS winning_trades,
    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losing_trades,
    ROUND(
        (SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)::float /
         NULLIF(COUNT(*), 0) * 100)::numeric,
        2
    ) AS win_rate_pct,
    ROUND(SUM(pnl)::numeric, 4) AS total_pnl,
    ROUND(AVG(pnl)::numeric, 4) AS avg_pnl,
    ROUND(AVG(edge)::numeric, 4) AS avg_edge,
    ROUND(AVG(kelly_fraction)::numeric, 4) AS avg_kelly,
    MIN(created_at) AS first_trade,
    MAX(created_at) AS last_trade
FROM trade_logs
WHERE pnl IS NOT NULL;


-- ─── 7. Row Level Security (RLS) ────────────────────────────
-- Supabase'de RLS varsayilan olarak aktiftir.
-- Service Role Key ile erisim saglanacagi icin,
-- bu tablolarda policy tanimlamasi gerekebilir.

ALTER TABLE bot_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE market_opportunities ENABLE ROW LEVEL SECURITY;
ALTER TABLE trade_logs ENABLE ROW LEVEL SECURITY;

-- Service role icin tam erisim politikasi
CREATE POLICY "Service role full access" ON bot_state
    FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "Service role full access" ON positions
    FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "Service role full access" ON orders
    FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "Service role full access" ON market_opportunities
    FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "Service role full access" ON trade_logs
    FOR ALL USING (true) WITH CHECK (true);


-- ═══════════════════════════════════════════════════════════════
-- Sema olusturma tamamlandi.
-- Supabase SQL Editor'de calistirdiktan sonra, GitHub Secrets'a
-- SUPABASE_URL ve SUPABASE_KEY (service_role) degerlerini ekleyin.
-- ═══════════════════════════════════════════════════════════════
