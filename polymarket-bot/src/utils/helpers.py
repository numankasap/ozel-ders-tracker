"""
Yardimci Fonksiyonlar

Token ID donusumleri, fiyat formatlama, zaman hesaplamalari
ve diger ortak islevler.
"""

import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Fiyat ve Miktar Formatlama ──────────────────────────────────

def format_price(price: float) -> float:
    """Polymarket fiyat formatina yuvarlar (2 ondalik basamak, 0.01-0.99)."""
    price = round(price, 2)
    return max(0.01, min(0.99, price))


def format_size(size: float) -> float:
    """Emir boyutunu formatlar (2 ondalik, min $1)."""
    size = round(size, 2)
    return max(0.0, size)


def price_to_probability(price: float) -> float:
    """Polymarket fiyatini olasilik yuzdesi olarak dondurur."""
    return round(price * 100, 2)


def probability_to_price(probability_pct: float) -> float:
    """Olasilik yuzdesini Polymarket fiyatina cevirir."""
    return round(probability_pct / 100.0, 2)


# ─── Token ve Piyasa Yardimcilari ───────────────────────────────

def get_yes_token(market: dict) -> Optional[dict]:
    """Piyasa verisinden 'Yes' tokenini dondurur."""
    for token in market.get("tokens", []):
        outcome = token.get("outcome", "").lower()
        if outcome in ("yes", "evet"):
            return token
    # Ilk token genellikle Yes token'dir
    tokens = market.get("tokens", [])
    return tokens[0] if tokens else None


def get_no_token(market: dict) -> Optional[dict]:
    """Piyasa verisinden 'No' tokenini dondurur."""
    for token in market.get("tokens", []):
        outcome = token.get("outcome", "").lower()
        if outcome in ("no", "hayir"):
            return token
    tokens = market.get("tokens", [])
    return tokens[1] if len(tokens) > 1 else None


def calculate_implied_probability(tokens: list[dict]) -> dict:
    """Token fiyatlarindan ima edilen olasiliklari hesaplar."""
    result = {}
    total = sum(t.get("price", 0) for t in tokens)
    for token in tokens:
        price = token.get("price", 0)
        outcome = token.get("outcome", "Unknown")
        # Normalize (fiyatlar 1'i gecebilir veya altinda kalabilir)
        normalized = price / total if total > 0 else 0
        result[outcome] = {
            "raw_price": price,
            "normalized": round(normalized, 4),
        }
    return result


def detect_arbitrage(tokens: list[dict]) -> Optional[dict]:
    """
    CTF arbitraj firsati tespit eder.

    Eger tum sonuclarin toplam fiyati 1.00$'dan farkli ise,
    risksiz kar firsati var demektir.

    Returns:
        {"opportunity": True, "total": 0.95, "profit": 0.05} veya None
    """
    total_price = sum(t.get("price", 0) for t in tokens)

    if total_price < 0.98:
        # Ucuza al, kesin kar
        profit = 1.0 - total_price
        return {
            "opportunity": True,
            "type": "underpriced",
            "total_price": round(total_price, 4),
            "profit_per_set": round(profit, 4),
        }
    elif total_price > 1.02:
        # Pahaliya sat, kesin kar
        profit = total_price - 1.0
        return {
            "opportunity": True,
            "type": "overpriced",
            "total_price": round(total_price, 4),
            "profit_per_set": round(profit, 4),
        }

    return None


# ─── Zaman Yardimcilari ─────────────────────────────────────────

def parse_iso_datetime(date_str: str) -> Optional[datetime]:
    """ISO format tarih stringini datetime'a cevirir."""
    if not date_str:
        return None
    try:
        # "Z" sonekini UTC'ye cevir
        date_str = date_str.replace("Z", "+00:00")
        return datetime.fromisoformat(date_str)
    except ValueError:
        return None


def time_until_expiry(end_date_str: str) -> Optional[timedelta]:
    """Piyasa bitis tarihine kalan sureyi hesaplar."""
    end_date = parse_iso_datetime(end_date_str)
    if not end_date:
        return None
    now = datetime.now(timezone.utc)
    return end_date - now


def is_market_expiring_soon(end_date_str: str, hours: int = 6) -> bool:
    """Piyasa belirtilen saat icinde kapanacak mi?"""
    remaining = time_until_expiry(end_date_str)
    if remaining is None:
        return False
    return remaining < timedelta(hours=hours)


def is_market_too_far(end_date_str: str, days: int = 180) -> bool:
    """Piyasa bitis tarihi cok uzak mi?"""
    remaining = time_until_expiry(end_date_str)
    if remaining is None:
        return True
    return remaining > timedelta(days=days)


def format_duration(td: timedelta) -> str:
    """timedelta'yi okunabilir formata cevirir."""
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        return "Suresi dolmus"

    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    if days > 0:
        return f"{days}g {hours}s"
    elif hours > 0:
        return f"{hours}s {minutes}dk"
    else:
        return f"{minutes}dk"


# ─── Piyasa Filtreleme ──────────────────────────────────────────

def filter_markets(
    markets: list[dict],
    min_volume: float = 10000,
    min_liquidity: float = 5000,
    max_expiry_days: int = 180,
    min_expiry_hours: int = 6,
    allowed_tags: Optional[list[str]] = None,
    blocked_tags: Optional[list[str]] = None,
) -> list[dict]:
    """
    Piyasalari cesitli kriterlere gore filtreler (Dikkat Mekanizmasi).

    Args:
        markets: Ham piyasa listesi
        min_volume: Minimum hacim ($)
        min_liquidity: Minimum likidite ($)
        max_expiry_days: Maksimum bitis suresi (gun)
        min_expiry_hours: Minimum bitis suresi (saat)
        allowed_tags: Izin verilen etiketler (bos ise hepsi)
        blocked_tags: Engellenen etiketler

    Returns:
        Filtrelenmis piyasa listesi
    """
    filtered = []

    for market in markets:
        # Kapanmis mi?
        if market.get("closed", False):
            continue

        # Hacim filtresi
        if market.get("volume", 0) < min_volume:
            continue

        # Likidite filtresi
        if market.get("liquidity", 0) < min_liquidity:
            continue

        # Zaman filtresi
        end_date = market.get("end_date", "")
        if end_date:
            if is_market_expiring_soon(end_date, min_expiry_hours):
                continue
            if is_market_too_far(end_date, max_expiry_days):
                continue

        # Etiket filtresi
        tags = [t.lower() for t in market.get("tags", [])]
        if allowed_tags:
            allowed_lower = [t.lower() for t in allowed_tags]
            if not any(t in allowed_lower for t in tags):
                continue
        if blocked_tags:
            blocked_lower = [t.lower() for t in blocked_tags]
            if any(t in blocked_lower for t in tags):
                continue

        filtered.append(market)

    logger.info(
        f"Piyasa filtreleme: {len(markets)} -> {len(filtered)} "
        f"(min_vol=${min_volume}, min_liq=${min_liquidity})"
    )
    return filtered


# ─── Loglama Yardimcilari ───────────────────────────────────────

def setup_logging(level: str = "INFO") -> None:
    """Standart log formatini kurar."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def log_trade_summary(
    action: str,
    market_question: str,
    side: str,
    size: float,
    price: float,
    ai_prob: float,
    edge: float,
) -> None:
    """Islem ozetini loglar."""
    logger.info(
        f"\n{'='*60}\n"
        f"  ISLEM: {action}\n"
        f"  Piyasa: {market_question[:60]}\n"
        f"  Yon: {side} | Boyut: ${size:.2f} | Fiyat: {price:.2f}\n"
        f"  AI Olaslik: {ai_prob:.2%} | Kenar: {edge:+.2%}\n"
        f"{'='*60}"
    )
