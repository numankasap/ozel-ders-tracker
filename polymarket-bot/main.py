"""
Polymarket Otonom Ticaret Botu - Ana Orkestrasyon

"Uyan - Senkronize Ol - Islem Yap - Kaydet - Sonlan"
(Wake - Sync - Act - Save - Die) mimarisi.

Bu dosya GitHub Actions tarafindan her 15 dakikada bir calistirilir.
Her calismada:
1. Ortam degiskenlerini yukler
2. Kill switch kontrolu yapar
3. Veritabanini (Supabase) blokzinciri ile senkronize eder
4. Piyasalari tarar ve filtreler
5. LLM ile analiz yapar
6. Risk degerlendirmesi yapar
7. Uygun emirleri gonderir
8. Tum durumu veritabanina kaydeder
9. Sonlanir (konteyner yok edilir)

Kullanim:
    python main.py
    python main.py --dry-run  # Gercek islem yapmadan test
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime, timezone

# Proje modulleri
from src.core.state import StateManager
from src.core.execution import ExecutionEngine
from src.agents.analyst import AnalystAgent
from src.agents.risk import RiskManager
from src.utils.helpers import (
    setup_logging,
    filter_markets,
    get_yes_token,
    get_no_token,
    detect_arbitrage,
    log_trade_summary,
    time_until_expiry,
    format_duration,
)

logger = logging.getLogger(__name__)

# ─── Sabitler ────────────────────────────────────────────────────

MAX_MARKETS_TO_ANALYZE = 5  # LLM maliyetini sinirla
MAX_TRADES_PER_CYCLE = 3  # Dongü basina max islem
STALE_ORDER_MINUTES = 60  # Eski emir iptal esigi (dakika)


def parse_args():
    parser = argparse.ArgumentParser(description="Polymarket Otonom Ticaret Botu")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Gercek islem yapmadan calis (test modu)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log seviyesi",
    )
    return parser.parse_args()


def phase_0_kill_switch(state: StateManager) -> bool:
    """
    Faz 0: Kill Switch Kontrolu

    Supabase'deki bot_state tablosunda is_alive=false ise,
    bot hicbir islem yapmadan sonlanir.
    """
    logger.info("=" * 60)
    logger.info("FAZ 0: Kill Switch Kontrolu")
    logger.info("=" * 60)

    if not state.is_alive():
        logger.warning("KILL SWITCH AKTIF - Bot durduruldu.")
        return False

    logger.info("Kill switch: AKTIF (bot calisiyor)")
    return True


def phase_1_reconciliation(
    state: StateManager, engine: ExecutionEngine
) -> dict:
    """
    Faz 1: Durum Mutabakati (Reconciliation)

    Veritabanindaki pozisyonlari blokzinciri ile karsilastirir.
    Hayalet pozisyonlari temizler, yeni kesfedilenleri ekler.
    """
    logger.info("=" * 60)
    logger.info("FAZ 1: Durum Mutabakati (Senkronizasyon)")
    logger.info("=" * 60)

    # 1. Gercek bakiyeyi cek
    wallet = engine.get_wallet_balance()
    real_balance = wallet.get("usdc", 0.0)
    logger.info(f"Cüzdan bakiyesi: ${real_balance:.2f} USDC")

    # Baslangic sermayesini kaydet (ilk calismada)
    state.set_initial_balance(real_balance)
    initial = state.get_initial_balance()
    logger.info(f"Baslangic sermayesi: ${initial:.2f} USDC")

    # Bakiyeyi guncelle
    state.update_balance(usdc=real_balance)

    # 2. Pozisyon mutabakati
    onchain_positions = engine.get_positions()
    reconciliation = state.reconcile_positions(onchain_positions)

    logger.info(
        f"Mutabakat sonucu: "
        f"Eklenen={len(reconciliation['added'])}, "
        f"Kaldirilan={len(reconciliation['removed'])}, "
        f"Guncellenen={len(reconciliation['updated'])}"
    )

    # 3. Eski acik emirleri kontrol et ve iptal et
    stale_count = _cancel_stale_orders(state, engine)
    if stale_count > 0:
        logger.info(f"{stale_count} eski emir iptal edildi.")

    return {
        "balance": real_balance,
        "initial_balance": initial,
        "positions": onchain_positions,
        "reconciliation": reconciliation,
    }


def _cancel_stale_orders(state: StateManager, engine: ExecutionEngine) -> int:
    """Uzun suredir acik kalan emirleri iptal eder."""
    db_orders = state.get_open_orders()
    cancelled = 0
    now = datetime.now(timezone.utc)

    for order in db_orders:
        created_str = order.get("created_at", "")
        if not created_str:
            continue

        try:
            created = datetime.fromisoformat(
                created_str.replace("Z", "+00:00")
            )
            age_minutes = (now - created).total_seconds() / 60

            if age_minutes > STALE_ORDER_MINUTES:
                order_id = order.get("order_id", "")
                if order_id:
                    success = engine.cancel_order(order_id)
                    if success:
                        state.update_order_status(order_id, "CANCELLED")
                        cancelled += 1
        except (ValueError, TypeError):
            continue

    return cancelled


def phase_2_discovery(engine: ExecutionEngine, config: dict) -> list[dict]:
    """
    Faz 2: Piyasa Kesfetme ve Filtreleme (Dikkat Mekanizmasi)

    Gamma API'dan piyasalari ceker ve huniden gecirir:
    Likidite -> Zaman -> Etiket -> Siralanmis liste
    """
    logger.info("=" * 60)
    logger.info("FAZ 2: Piyasa Kesfetme ve Filtreleme")
    logger.info("=" * 60)

    raw_markets = engine.discover_markets(
        active=True,
        limit=50,
        min_volume=config.get("min_volume", 10000),
    )

    if not raw_markets:
        logger.warning("Hicbir piyasa bulunamadi.")
        return []

    # Filtrele
    filtered = filter_markets(
        raw_markets,
        min_volume=config.get("min_volume", 10000),
        min_liquidity=config.get("min_liquidity", 5000),
        max_expiry_days=config.get("max_expiry_days", 180),
        min_expiry_hours=config.get("min_expiry_hours", 6),
        allowed_tags=config.get("allowed_tags"),
        blocked_tags=config.get("blocked_tags"),
    )

    # Hacme gore sirala (en likit piyasalar once)
    filtered.sort(key=lambda m: m.get("volume", 0), reverse=True)

    # Analiz edilecek sayiyi sinirla
    selected = filtered[:MAX_MARKETS_TO_ANALYZE]

    for m in selected:
        remaining = time_until_expiry(m.get("end_date", ""))
        dur_str = format_duration(remaining) if remaining else "Bilinmiyor"
        yes_token = get_yes_token(m)
        yes_price = yes_token["price"] if yes_token else 0
        logger.info(
            f"  [{m.get('condition_id', '')[:8]}] "
            f"{m['question'][:60]}... "
            f"(YES: {yes_price:.2f}, Hacim: ${m.get('volume', 0):,.0f}, "
            f"Kalan: {dur_str})"
        )

    return selected


def phase_3_analysis(
    markets: list[dict],
    analyst: AnalystAgent,
    state: StateManager,
) -> list[dict]:
    """
    Faz 3: LLM ile Piyasa Analizi

    Her piyasa icin:
    - Onbellek kontrolu (son 4 saatte analiz edildi mi?)
    - Haber toplama (RAG)
    - LLM ile olasilik tahmini (CoT)
    - Sonuclari onbellege yazma
    """
    logger.info("=" * 60)
    logger.info("FAZ 3: LLM Analizi (Super Tahmincilik)")
    logger.info("=" * 60)

    opportunities = []

    for market in markets:
        condition_id = market.get("condition_id", "")
        question = market.get("question", "")

        # Onbellek kontrolu
        cached = state.get_cached_analysis(condition_id, max_age_hours=4)
        if cached:
            logger.info(f"  Onbellekten yuklendi: {question[:50]}...")
            opportunities.append(cached)
            continue

        # LLM analizi
        yes_token = get_yes_token(market)
        if not yes_token:
            continue

        market_price = yes_token.get("price", 0.5)

        try:
            analysis = analyst.analyze_market(
                question=question,
                market_price=market_price,
                description=market.get("description", ""),
            )

            opportunity = {
                "condition_id": condition_id,
                "question": question,
                "market_price": market_price,
                "ai_probability": analysis["ai_probability"],
                "edge": analysis["edge"],
                "confidence": analysis["confidence"],
                "rationale": analysis["rationale"],
                "news_summary": analysis["news_summary"],
                "tokens": market.get("tokens", []),
                "volume": market.get("volume", 0),
            }

            # Onbellege yaz
            state.cache_analysis(
                {
                    "condition_id": condition_id,
                    "question": question,
                    "market_price": market_price,
                    "ai_probability": analysis["ai_probability"],
                    "news_summary": analysis["news_summary"],
                    "trade_action": "PENDING",
                }
            )

            opportunities.append(opportunity)

            # API rate limit korumalari
            time.sleep(1)

        except Exception as e:
            logger.error(f"Analiz hatasi ({question[:40]}): {e}")
            continue

    # Edge'e gore sirala (en buyuk firsat once)
    opportunities.sort(key=lambda o: abs(o.get("edge", 0)), reverse=True)

    logger.info(f"{len(opportunities)} piyasa analiz edildi.")
    return opportunities


def phase_4_decision_and_execution(
    opportunities: list[dict],
    risk_mgr: RiskManager,
    engine: ExecutionEngine,
    state: StateManager,
    analyst: AnalystAgent,
    balance: float,
    initial_balance: float,
    positions: list[dict],
    dry_run: bool = False,
) -> list[dict]:
    """
    Faz 4: Karar Verme ve Islem Yurutme

    Her firsat icin:
    1. Risk degerlendirmesi (Kelly + Sermaye koruma)
    2. Islem yonu belirleme (BUY YES / BUY NO)
    3. Emir gonderme (veya dry-run'da simule etme)
    4. Sonuclari kaydetme
    """
    logger.info("=" * 60)
    logger.info("FAZ 4: Karar ve Islem Yurutme")
    logger.info("=" * 60)

    # Portfoy saglik kontrolu
    health = risk_mgr.portfolio_health_check(balance, initial_balance, positions)
    logger.info(
        f"Portfoy Durumu: {health['status'].upper()} "
        f"(Bakiye Orani: {health['balance_ratio']:.2%}, "
        f"Acik Pozisyon: {health['open_position_count']})"
    )
    logger.info(f"  Oneri: {health['recommendation']}")

    if health["status"] == "dead":
        logger.error("AJAN OLDU - Sermaye tukendi. Islem yapilmiyor.")
        state.set_config("config", {"is_alive": False, "death_reason": "bankrupt"})
        return []

    executed_trades = []
    trade_count = 0

    for opp in opportunities:
        if trade_count >= MAX_TRADES_PER_CYCLE:
            logger.info(f"Dongü basina max islem limitine ulasildi ({MAX_TRADES_PER_CYCLE}).")
            break

        condition_id = opp.get("condition_id", "")
        question = opp.get("question", "")
        ai_prob = opp.get("ai_probability", 0.5)
        market_price = opp.get("market_price", 0.5)
        edge = opp.get("edge", 0)
        tokens = opp.get("tokens", [])

        # Islem yonunu belirle
        if edge > 0:
            # AI olasiligi piyasadan yuksek -> YES al
            side = "BUY"
            target_token = get_yes_token({"tokens": tokens})
            buy_price = market_price
        else:
            # AI olasiligi piyasadan dusuk -> NO al (ters pozisyon)
            side = "BUY"
            target_token = get_no_token({"tokens": tokens})
            buy_price = 1.0 - market_price
            # Edge'i pozitife cevir (NO icin)
            ai_prob_for_risk = 1.0 - ai_prob
            edge = abs(edge)

        if not target_token or not target_token.get("token_id"):
            logger.warning(f"Token bulunamadi: {question[:40]}")
            continue

        token_id = target_token["token_id"]

        # Mevcut pozisyon buyuklugunu bul
        existing_size = 0
        for pos in positions:
            if (
                pos.get("condition_id") == condition_id
                and pos.get("token_id") == token_id
            ):
                existing_size = pos.get("size", 0)
                break

        # Risk degerlendirmesi
        assessment = risk_mgr.assess_trade(
            ai_probability=ai_prob if edge > 0 else ai_prob_for_risk,
            market_price=buy_price,
            current_balance=balance,
            initial_balance=initial_balance,
            existing_position_size=existing_size,
            side=side,
        )

        logger.info(
            f"\n  Piyasa: {question[:50]}...\n"
            f"  AI: {ai_prob:.2%} vs Piyasa: {market_price:.2%} "
            f"(Edge: {opp.get('edge', 0):+.2%})\n"
            f"  Risk: {assessment.reason}\n"
            f"  Sonuc: {'ISLEM YAP' if assessment.should_trade else 'PAS GEC'}"
        )

        if not assessment.should_trade:
            continue

        order_size = assessment.order_size_usdc

        if dry_run:
            logger.info(
                f"  [DRY-RUN] Emir SIMULE edildi: "
                f"{side} ${order_size:.2f} @ {buy_price:.2f}"
            )
            executed_trades.append(
                {
                    "condition_id": condition_id,
                    "question": question,
                    "side": side,
                    "size": order_size,
                    "price": buy_price,
                    "dry_run": True,
                }
            )
            trade_count += 1
            continue

        # Gercek emir gonder
        order_result = engine.place_order(
            token_id=token_id,
            price=buy_price,
            size=order_size,
            side=side,
            order_type="GTC",
        )

        if order_result:
            order_id = order_result.get("order_id", "unknown")

            # Veritabanina kaydet
            state.save_order(
                {
                    "order_id": order_id,
                    "market_id": condition_id,
                    "token_id": token_id,
                    "side": side,
                    "size": order_size,
                    "price": buy_price,
                    "status": "OPEN",
                }
            )

            # Islem gerekce logu
            rationale = analyst.generate_trade_rationale(
                question=question,
                side=f"{side} {'YES' if edge > 0 else 'NO'}",
                ai_prob=ai_prob,
                market_price=market_price,
            )

            state.log_trade(
                {
                    "order_id": order_id,
                    "condition_id": condition_id,
                    "question": question,
                    "side": side,
                    "size": order_size,
                    "price": buy_price,
                    "ai_probability": ai_prob,
                    "market_price": market_price,
                    "edge": opp.get("edge", 0),
                    "kelly_fraction": assessment.kelly_fraction,
                    "rationale": rationale,
                    "risk_level": assessment.risk_level,
                }
            )

            log_trade_summary(
                action="EMIR GONDERILDI",
                market_question=question,
                side=f"{side} {'YES' if edge > 0 else 'NO'}",
                size=order_size,
                price=buy_price,
                ai_prob=ai_prob,
                edge=opp.get("edge", 0),
            )

            executed_trades.append(order_result)
            trade_count += 1

            # Bakiyeyi guncelle (islem sonrasi)
            balance -= order_size

    return executed_trades


def phase_5_arbitrage_check(
    markets: list[dict], engine: ExecutionEngine, dry_run: bool = False
) -> list[dict]:
    """
    Faz 5: CTF Arbitraj Kontrolu

    Tum sonuclarin toplam fiyati < 0.98 ise risksiz kar firsati var.
    Split/Merge mekanizmasi ile deger elde edilir.
    """
    logger.info("=" * 60)
    logger.info("FAZ 5: Arbitraj Taramasi")
    logger.info("=" * 60)

    arb_opportunities = []

    for market in markets:
        tokens = market.get("tokens", [])
        arb = detect_arbitrage(tokens)

        if arb:
            logger.info(
                f"  ARBITRAJ FIRSATI: {market.get('question', '')[:50]}... "
                f"Toplam: {arb['total_price']:.4f}, "
                f"Kar/Set: ${arb['profit_per_set']:.4f}"
            )
            arb_opportunities.append(
                {
                    "market": market,
                    "arbitrage": arb,
                }
            )

    if not arb_opportunities:
        logger.info("  Arbitraj firsati bulunamadi.")

    return arb_opportunities


def phase_6_summary(
    state: StateManager,
    balance: float,
    initial_balance: float,
    trades: list[dict],
    start_time: float,
) -> None:
    """
    Faz 6: Ozet ve Kapaniss

    Dongu istatistiklerini loglar ve veritabanina kaydeder.
    """
    elapsed = time.time() - start_time

    logger.info("=" * 60)
    logger.info("FAZ 6: Dongu Ozeti")
    logger.info("=" * 60)

    pnl_pct = ((balance / initial_balance) - 1) * 100 if initial_balance > 0 else 0

    logger.info(f"  Calisma Suresi: {elapsed:.1f} saniye")
    logger.info(f"  Bakiye: ${balance:.2f} USDC")
    logger.info(f"  Baslangic: ${initial_balance:.2f} USDC")
    logger.info(f"  PnL: {pnl_pct:+.2f}%")
    logger.info(f"  Bu Dongude Yapilan Islem: {len(trades)}")
    logger.info(f"  Zaman: {datetime.now(timezone.utc).isoformat()}")

    # Son calisma bilgisini kaydet
    state.set_config(
        "last_run",
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "balance": balance,
            "trades_count": len(trades),
            "elapsed_seconds": round(elapsed, 1),
            "pnl_pct": round(pnl_pct, 2),
        },
    )

    logger.info("=" * 60)
    logger.info("BOT DONGUSU TAMAMLANDI - Sonlaniyor...")
    logger.info("=" * 60)


# ─── Ana Giris Noktasi ──────────────────────────────────────────


def main():
    args = parse_args()
    setup_logging(args.log_level)
    start_time = time.time()

    logger.info("=" * 60)
    logger.info("  POLYMARKET OTONOM TICARET BOTU")
    logger.info("  Mimari: Uyan-Senkronize Ol-Islem Yap-Kaydet-Sonlan")
    logger.info(f"  Mod: {'DRY-RUN (Test)' if args.dry_run else 'CANLI (Live)'}")
    logger.info(f"  Zaman: {datetime.now(timezone.utc).isoformat()}")
    logger.info("=" * 60)

    try:
        # ─── Bilesenleri Baslat ──────────────────────────────────
        state = StateManager()
        engine = ExecutionEngine()
        analyst = AnalystAgent()

        risk_params = state.get_risk_params()
        risk_mgr = RiskManager(risk_params)

        # Konfigurasyon yukle
        market_config = state.get_config("market_config", {})
        if not isinstance(market_config, dict):
            market_config = {}

        # Varsayilan konfigurasyonu doldur
        market_config.setdefault("min_volume", 10000)
        market_config.setdefault("min_liquidity", 5000)
        market_config.setdefault("max_expiry_days", 180)
        market_config.setdefault("min_expiry_hours", 6)
        market_config.setdefault("allowed_tags", None)
        market_config.setdefault("blocked_tags", None)

        # ─── Faz 0: Kill Switch ──────────────────────────────────
        if not phase_0_kill_switch(state):
            return

        # ─── Faz 1: Durum Mutabakati ────────────────────────────
        sync_result = phase_1_reconciliation(state, engine)
        balance = sync_result["balance"]
        initial_balance = sync_result["initial_balance"]
        positions = sync_result["positions"]

        if balance <= 0:
            logger.error("Bakiye sifir veya negatif. Bot sonlaniyor.")
            state.set_config(
                "config", {"is_alive": False, "death_reason": "zero_balance"}
            )
            return

        # ─── Faz 2: Piyasa Kesfetme ─────────────────────────────
        markets = phase_2_discovery(engine, market_config)
        if not markets:
            logger.info("Filtre sonrasi piyasa kalmadi. Dongu sonlaniyor.")
            phase_6_summary(state, balance, initial_balance, [], start_time)
            return

        # ─── Faz 3: LLM Analizi ─────────────────────────────────
        opportunities = phase_3_analysis(markets, analyst, state)
        if not opportunities:
            logger.info("Analiz sonrasi firsat bulunamadi. Dongu sonlaniyor.")
            phase_6_summary(state, balance, initial_balance, [], start_time)
            return

        # ─── Faz 4: Karar ve Islem ──────────────────────────────
        trades = phase_4_decision_and_execution(
            opportunities=opportunities,
            risk_mgr=risk_mgr,
            engine=engine,
            state=state,
            analyst=analyst,
            balance=balance,
            initial_balance=initial_balance,
            positions=positions,
            dry_run=args.dry_run,
        )

        # ─── Faz 5: Arbitraj Taramasi ───────────────────────────
        phase_5_arbitrage_check(markets, engine, dry_run=args.dry_run)

        # ─── Faz 6: Ozet ve Kapaniss ────────────────────────────
        # Bakiyeyi yeniden cek (islemler sonrasi)
        if trades and not args.dry_run:
            wallet = engine.get_wallet_balance()
            balance = wallet.get("usdc", balance)
            state.update_balance(usdc=balance)

        phase_6_summary(state, balance, initial_balance, trades, start_time)

    except Exception as e:
        logger.critical(f"KRITIK HATA: {e}", exc_info=True)
        # Kritik hatada bile durumu kaydetmeye calis
        try:
            state.set_config(
                "last_error",
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "error": str(e),
                    "type": type(e).__name__,
                },
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
