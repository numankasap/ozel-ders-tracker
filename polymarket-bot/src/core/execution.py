"""
CLOB Islem Motoru - Ajanin Elleri

Polymarket CLOB API uzerinden:
- L2 kimlik dogrulama (API anahtar turetme)
- Emir olusturma ve gonderme (Limit, FOK, IOC)
- Emir iptali
- Emir defteri (orderbook) sorgulama
- Bakiye ve pozisyon sorgulama (Data API)

py-clob-client kutuphanesi, karmasik EIP-712 imzalama surecini
soyutlar. Bot, ozel anahtar ile API anahtari tureterek ticaret
yapar.
"""

import os
import time
import logging
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# ─── py-clob-client Import ──────────────────────────────────────
# Bu kutuphanenin yuklu olmasi gerekir: pip install py-clob-client
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds,
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
    )
    from py_clob_client.constants import POLYGON

    HAS_CLOB_CLIENT = True
except ImportError:
    HAS_CLOB_CLIENT = False
    logger.warning(
        "py-clob-client yuklu degil. Islem yapma devre disi. "
        "pip install py-clob-client"
    )

# ─── Sabitler ────────────────────────────────────────────────────

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
DATA_HOST = "https://data-api.polymarket.com"
CHAIN_ID = POLYGON if HAS_CLOB_CLIENT else 137

# API Rate Limit korumalari (saniye)
REQUEST_DELAY = 0.5


class ExecutionEngine:
    """
    Polymarket CLOB API istem motoru.

    Sorumluluklar:
    - CLOB istemcisi baslatma ve L2 kimlik dogrulama
    - Piyasa kesfetme (Gamma API)
    - Fiyat ve emir defteri sorgulama (CLOB API)
    - Emir gonderme ve iptal (CLOB API)
    - Bakiye ve pozisyon sorgulama (Data API)
    """

    def __init__(self):
        self.private_key = os.getenv("POLY_PRIVATE_KEY")
        if not self.private_key:
            raise EnvironmentError(
                "POLY_PRIVATE_KEY ortam degiskeni tanimlanmali."
            )

        self.client: Optional[ClobClient] = None
        self.api_creds: Optional[ApiCreds] = None

        if HAS_CLOB_CLIENT:
            self._initialize_client()
        else:
            logger.error("CLOB istemcisi yuklenemedi. Islem yapilamaz.")

    def _initialize_client(self) -> None:
        """CLOB istemcisini baslatir ve L2 kimlik dogrulama yapar."""
        try:
            self.client = ClobClient(
                CLOB_HOST,
                key=self.private_key,
                chain_id=CHAIN_ID,
                signature_type=0,  # EOA
            )

            # L2 API anahtari tureti (veya mevcut olanini kullan)
            self.api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(self.api_creds)

            logger.info(
                "CLOB istemcisi baslatildi. L2 kimlik dogrulama basarili."
            )
        except Exception as e:
            logger.error(f"CLOB istemcisi baslatma hatasi: {e}")
            raise

    # ─── Piyasa Kesfetme (Gamma API) ────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def discover_markets(
        self,
        active: bool = True,
        limit: int = 20,
        min_volume: float = 10000,
        tag: str = "",
    ) -> list[dict]:
        """
        Gamma API ile aktif piyasalari kesfeder.

        Args:
            active: Sadece aktif piyasalar
            limit: Maksimum sonuc sayisi
            min_volume: Minimum hacim filtresi ($)
            tag: Etiket filtresi (ornegin: "crypto")

        Returns:
            Piyasa listesi
        """
        params = {
            "active": str(active).lower(),
            "limit": limit,
            "order": "volume",
            "ascending": "false",
        }
        if tag:
            params["tag"] = tag

        try:
            resp = requests.get(
                f"{GAMMA_HOST}/events",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()

            markets = []
            for event in events:
                for market in event.get("markets", []):
                    volume = float(market.get("volume", 0) or 0)
                    if volume >= min_volume:
                        markets.append(self._normalize_market(event, market))

            time.sleep(REQUEST_DELAY)
            logger.info(f"{len(markets)} piyasa kesfedildi (filtre sonrasi).")
            return markets

        except Exception as e:
            logger.error(f"Piyasa kesfetme hatasi: {e}")
            return []

    def _normalize_market(self, event: dict, market: dict) -> dict:
        """Gamma API yaniti normalize eder."""
        outcomes = market.get("outcomes", [])
        outcome_prices = market.get("outcomePrices", [])

        # Fiyat ve token bilgilerini parse et
        tokens = []
        if isinstance(outcome_prices, str):
            try:
                import json

                outcome_prices = json.loads(outcome_prices)
            except Exception:
                outcome_prices = []

        clobTokenIds = market.get("clobTokenIds", [])
        if isinstance(clobTokenIds, str):
            try:
                import json

                clobTokenIds = json.loads(clobTokenIds)
            except Exception:
                clobTokenIds = []

        for i, outcome in enumerate(outcomes):
            price = float(outcome_prices[i]) if i < len(outcome_prices) else 0
            token_id = clobTokenIds[i] if i < len(clobTokenIds) else ""
            tokens.append(
                {"outcome": outcome, "price": price, "token_id": token_id}
            )

        return {
            "condition_id": market.get("conditionId", ""),
            "question_id": market.get("questionID", ""),
            "question": market.get("question", event.get("title", "")),
            "description": market.get("description", "")[:500],
            "end_date": market.get("endDate", ""),
            "volume": float(market.get("volume", 0) or 0),
            "liquidity": float(market.get("liquidity", 0) or 0),
            "tokens": tokens,
            "tags": event.get("tags", []),
            "active": market.get("active", True),
            "closed": market.get("closed", False),
        }

    # ─── Fiyat Sorgulama (CLOB API) ─────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_price(self, token_id: str, side: str = "buy") -> Optional[float]:
        """
        Token icin guncel fiyati dondurur.

        Args:
            token_id: CLOB token ID
            side: "buy" veya "sell"
        """
        if not self.client:
            return None

        try:
            resp = self.client.get_price(token_id, side)
            time.sleep(REQUEST_DELAY)
            return float(resp.get("price", 0))
        except Exception as e:
            logger.error(f"Fiyat sorgulama hatasi (token={token_id[:16]}): {e}")
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_orderbook(self, token_id: str) -> Optional[dict]:
        """Emir defteri derinligini sorgular."""
        if not self.client:
            return None

        try:
            book = self.client.get_order_book(token_id)
            time.sleep(REQUEST_DELAY)
            return book
        except Exception as e:
            logger.error(
                f"Emir defteri sorgulama hatasi (token={token_id[:16]}): {e}"
            )
            return None

    # ─── Emir Gonderme ──────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=2, min=3, max=15),
    )
    def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        order_type: str = "GTC",
    ) -> Optional[dict]:
        """
        CLOB uzerinden emir gonderir.

        Args:
            token_id: Hedef token ID
            price: Limit fiyat (0.01-0.99)
            size: Miktar (USDC cinsinden)
            side: "BUY" veya "SELL"
            order_type: "GTC" (Good Til Cancel), "FOK" (Fill or Kill),
                        "GTD" (Good Til Date)

        Returns:
            Emir bilgileri veya None (basarisiz)
        """
        if not self.client:
            logger.error("CLOB istemcisi baslatilamadi. Emir gonderilemez.")
            return None

        # Fiyat formatlama (Polymarket 2 ondalik basamak kabul eder)
        price = round(price, 2)
        price = max(0.01, min(0.99, price))

        # Boyut kontrolu
        if size < 1.0:
            logger.warning(f"Emir boyutu cok kucuk: ${size:.2f}")
            return None

        clob_side = "BUY" if side.upper() == "BUY" else "SELL"

        try:
            order_args = OrderArgs(
                price=price,
                size=size,
                side=clob_side,
                token_id=token_id,
            )

            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, order_type)

            order_id = None
            if isinstance(result, dict):
                order_id = result.get("orderID") or result.get("id")

            logger.info(
                f"Emir gonderildi: {clob_side} {size:.2f}@{price:.2f} "
                f"(token={token_id[:16]}..., order_id={order_id})"
            )
            time.sleep(REQUEST_DELAY)

            return {
                "order_id": order_id,
                "token_id": token_id,
                "side": clob_side,
                "price": price,
                "size": size,
                "type": order_type,
                "raw_response": result,
            }

        except Exception as e:
            logger.error(
                f"Emir gonderme hatasi: {e} "
                f"(token={token_id[:16]}, {clob_side} {size}@{price})"
            )
            return None

    # ─── Emir Iptali ─────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=2, max=8),
    )
    def cancel_order(self, order_id: str) -> bool:
        """Acik bir emri iptal eder."""
        if not self.client:
            return False

        try:
            self.client.cancel(order_id)
            logger.info(f"Emir iptal edildi: {order_id}")
            time.sleep(REQUEST_DELAY)
            return True
        except Exception as e:
            logger.error(f"Emir iptal hatasi (order_id={order_id}): {e}")
            return False

    def cancel_all_orders(self) -> int:
        """Tum acik emirleri iptal eder. Iptal edilen emir sayisini dondurur."""
        if not self.client:
            return 0

        try:
            self.client.cancel_all()
            logger.info("Tum acik emirler iptal edildi.")
            return -1  # API tam sayi donmuyor
        except Exception as e:
            logger.error(f"Toplu emir iptal hatasi: {e}")
            return 0

    # ─── Acik Emir Sorgulama ─────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_open_orders(self) -> list[dict]:
        """CLOB'daki acik emirleri listeler."""
        if not self.client:
            return []

        try:
            orders = self.client.get_orders()
            time.sleep(REQUEST_DELAY)
            if isinstance(orders, list):
                return orders
            return []
        except Exception as e:
            logger.error(f"Acik emir sorgulama hatasi: {e}")
            return []

    # ─── Bakiye ve Pozisyon Sorgulama (Data API) ─────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_wallet_balance(self) -> dict:
        """
        Cüzdandaki USDC bakiyesini sorgular.

        Not: Polymarket proxy cuzdan kullaniyorsa, Data API
        uzerinden sorgulanir. EOA kullaniyorsa, RPC uzerinden
        USDC kontrat bakiyesi cekilir.
        """
        if not self.client:
            return {"usdc": 0.0}

        try:
            # py-clob-client uzerinden allowance/bakiye kontrolu
            # Gercek implementasyonda Polygon RPC veya Data API kullanilir
            balance_info = {"usdc": 0.0}

            # Polymarket Data API (eger erisilebilirse)
            try:
                address = self.client.get_address()
                resp = requests.get(
                    f"{DATA_HOST}/value",
                    params={"user": address},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    balance_info["usdc"] = float(
                        data.get("balance", 0)
                    )
            except Exception:
                pass

            time.sleep(REQUEST_DELAY)
            return balance_info

        except Exception as e:
            logger.error(f"Bakiye sorgulama hatasi: {e}")
            return {"usdc": 0.0}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_positions(self) -> list[dict]:
        """Cüzdandaki tum pozisyonlari listeler."""
        if not self.client:
            return []

        try:
            address = self.client.get_address()
            resp = requests.get(
                f"{DATA_HOST}/positions",
                params={"user": address},
                timeout=10,
            )
            if resp.status_code != 200:
                return []

            positions_raw = resp.json()
            positions = []

            for pos in positions_raw:
                size = float(pos.get("size", 0))
                if size > 0:
                    positions.append(
                        {
                            "condition_id": pos.get("conditionId", ""),
                            "token_id": pos.get("tokenId", ""),
                            "size": size,
                            "avg_price": float(pos.get("avgPrice", 0)),
                            "cur_price": float(pos.get("curPrice", 0)),
                            "pnl": float(pos.get("pnl", 0)),
                        }
                    )

            time.sleep(REQUEST_DELAY)
            return positions

        except Exception as e:
            logger.error(f"Pozisyon sorgulama hatasi: {e}")
            return []

    # ─── Spread Analizi ──────────────────────────────────────────

    def analyze_spread(self, token_id: str) -> Optional[dict]:
        """Emir defterindeki spread ve likiditeyi analiz eder."""
        book = self.get_orderbook(token_id)
        if not book:
            return None

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            return None

        best_bid = float(bids[0].get("price", 0))
        best_ask = float(asks[0].get("price", 0))
        spread = best_ask - best_bid
        mid_price = (best_bid + best_ask) / 2

        # Derinlik hesapla (ilk 5 seviye)
        bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
        ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": round(spread, 4),
            "spread_pct": round(spread / mid_price * 100, 2) if mid_price else 0,
            "mid_price": round(mid_price, 4),
            "bid_depth": round(bid_depth, 2),
            "ask_depth": round(ask_depth, 2),
        }
