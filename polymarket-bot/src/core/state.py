"""
Supabase Durum Yonetimi - Ajanin Dis Hipokampusu

Bot her GitHub Actions dongusunde sifirdan basladigindan,
tum durum verisi (bakiye, pozisyonlar, konfigurason) bu modul
uzerinden Supabase'e yazilir ve okunur.

"Uyan-Senkronize Ol-Islem Yap-Kaydet-Sonlan" mimarisinin
senkronizasyon katmani.
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from supabase import create_client, Client

logger = logging.getLogger(__name__)


class StateManager:
    """
    Supabase uzerinden bot durumunu yoneten sinif.

    Tablolar:
        - bot_state: Genel konfigurasyon ve durum degiskenleri (key-value)
        - positions: Acik pozisyonlar envanteri
        - orders: Emir tarihcesi ve durumlari
        - market_opportunities: LLM analiz onbellegi
        - trade_logs: Islem gerekceleri ve oz-yansitma verileri
    """

    def __init__(self):
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise EnvironmentError(
                "SUPABASE_URL ve SUPABASE_KEY ortam degiskenleri tanimlanmali."
            )
        self.client: Client = create_client(url, key)
        logger.info("Supabase baglantisi kuruldu.")

    # ─── Bot State (Konfigurasyon) ──────────────────────────────

    def get_config(self, key: str, default: Any = None) -> Any:
        """bot_state tablosundan bir konfigurasyon degerini okur."""
        try:
            result = (
                self.client.table("bot_state")
                .select("value")
                .eq("key", key)
                .execute()
            )
            if result.data:
                return result.data[0]["value"]
            return default
        except Exception as e:
            logger.error(f"Konfigurasyon okuma hatasi (key={key}): {e}")
            return default

    def set_config(self, key: str, value: Any) -> None:
        """bot_state tablosuna bir konfigurasyon degeri yazar (upsert)."""
        try:
            self.client.table("bot_state").upsert(
                {
                    "key": key,
                    "value": value,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()
        except Exception as e:
            logger.error(f"Konfigurasyon yazma hatasi (key={key}): {e}")
            raise

    def is_alive(self) -> bool:
        """Kill switch kontrolu. False donerse bot durur."""
        config = self.get_config("config", {"is_alive": True})
        if isinstance(config, dict):
            return config.get("is_alive", True)
        return True

    def get_risk_params(self) -> dict:
        """Risk yonetimi parametrelerini dondurur."""
        defaults = {
            "kelly_fraction": 0.25,
            "max_position_pct": 0.20,
            "max_single_order_usdc": 50.0,
            "stop_out_pct": 0.20,
            "min_edge": 0.10,
            "emergency_threshold_pct": 0.20,
            "emergency_min_confidence": 0.90,
        }
        stored = self.get_config("risk_params", {})
        if isinstance(stored, dict):
            defaults.update(stored)
        return defaults

    # ─── Bakiye Yonetimi ────────────────────────────────────────

    def get_balance(self) -> dict:
        """Son bilinen bakiyeyi dondurur."""
        return self.get_config("balance", {"usdc": 0.0, "matic": 0.0})

    def update_balance(self, usdc: float, matic: float = 0.0) -> None:
        """Bakiyeyi gunceller."""
        self.set_config("balance", {"usdc": usdc, "matic": matic})

    def get_initial_balance(self) -> float:
        """Baslangic sermayesini dondurur."""
        val = self.get_config("initial_balance", 0.0)
        if isinstance(val, dict):
            return val.get("usdc", 0.0)
        return float(val)

    def set_initial_balance(self, usdc: float) -> None:
        """Baslangic sermayesini kaydeder (sadece ilk calistiginda)."""
        existing = self.get_initial_balance()
        if existing <= 0:
            self.set_config("initial_balance", usdc)

    # ─── Pozisyon Yonetimi ──────────────────────────────────────

    def get_positions(self) -> list[dict]:
        """Tum acik pozisyonlari dondurur."""
        try:
            result = (
                self.client.table("positions")
                .select("*")
                .eq("is_open", True)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Pozisyon okuma hatasi: {e}")
            return []

    def upsert_position(self, position: dict) -> None:
        """Pozisyonu ekler veya gunceller."""
        try:
            position["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.client.table("positions").upsert(
                position, on_conflict="condition_id,token_id"
            ).execute()
        except Exception as e:
            logger.error(f"Pozisyon yazma hatasi: {e}")
            raise

    def close_position(self, condition_id: str, token_id: str) -> None:
        """Pozisyonu kapatir."""
        try:
            self.client.table("positions").update(
                {
                    "is_open": False,
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("condition_id", condition_id).eq("token_id", token_id).execute()
        except Exception as e:
            logger.error(f"Pozisyon kapatma hatasi: {e}")
            raise

    # ─── Emir Yonetimi ──────────────────────────────────────────

    def save_order(self, order: dict) -> None:
        """Yeni emri kaydeder."""
        try:
            order["created_at"] = datetime.now(timezone.utc).isoformat()
            self.client.table("orders").insert(order).execute()
        except Exception as e:
            logger.error(f"Emir kaydetme hatasi: {e}")
            raise

    def get_open_orders(self) -> list[dict]:
        """Acik (OPEN) emirleri dondurur."""
        try:
            result = (
                self.client.table("orders")
                .select("*")
                .eq("status", "OPEN")
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Acik emir okuma hatasi: {e}")
            return []

    def update_order_status(self, order_id: str, status: str) -> None:
        """Emir durumunu gunceller (FILLED, CANCELLED, vb.)."""
        try:
            self.client.table("orders").update(
                {
                    "status": status,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("order_id", order_id).execute()
        except Exception as e:
            logger.error(f"Emir guncelleme hatasi: {e}")
            raise

    # ─── Market Onbellegi ────────────────────────────────────────

    def get_cached_analysis(
        self, condition_id: str, max_age_hours: int = 4
    ) -> Optional[dict]:
        """Onceden analiz edilmis piyasa verisini dondurur (onbellek)."""
        try:
            result = (
                self.client.table("market_opportunities")
                .select("*")
                .eq("condition_id", condition_id)
                .execute()
            )
            if not result.data:
                return None

            record = result.data[0]
            analyzed_at = datetime.fromisoformat(
                record["last_analyzed"].replace("Z", "+00:00")
            )
            age_hours = (
                datetime.now(timezone.utc) - analyzed_at
            ).total_seconds() / 3600

            if age_hours > max_age_hours:
                return None
            return record
        except Exception as e:
            logger.error(f"Onbellek okuma hatasi: {e}")
            return None

    def cache_analysis(self, analysis: dict) -> None:
        """LLM analiz sonucunu onbellege yazar."""
        try:
            analysis["last_analyzed"] = datetime.now(timezone.utc).isoformat()
            self.client.table("market_opportunities").upsert(
                analysis, on_conflict="condition_id"
            ).execute()
        except Exception as e:
            logger.error(f"Onbellek yazma hatasi: {e}")

    # ─── Trade Log (Oz-Yansitma) ────────────────────────────────

    def log_trade(self, log_entry: dict) -> None:
        """Islem gerekce ve sonucunu kaydeder."""
        try:
            log_entry["created_at"] = datetime.now(timezone.utc).isoformat()
            self.client.table("trade_logs").insert(log_entry).execute()
        except Exception as e:
            logger.error(f"Trade log yazma hatasi: {e}")

    def get_recent_trades(self, limit: int = 20) -> list[dict]:
        """Son islemleri getirir (retrospektif analiz icin)."""
        try:
            result = (
                self.client.table("trade_logs")
                .select("*")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Trade log okuma hatasi: {e}")
            return []

    # ─── Durum Mutabakati (Reconciliation) ───────────────────────

    def reconcile_positions(self, onchain_positions: list[dict]) -> dict:
        """
        Veritabanindaki pozisyonlari blokzinciri verileriyle karsilastirir.

        Returns:
            {
                "added": [...],    # Zincirde var, DB'de yok
                "removed": [...],  # DB'de var, zincirde yok
                "updated": [...]   # Farkliliklar guncellendi
            }
        """
        db_positions = self.get_positions()
        db_map = {
            (p["condition_id"], p["token_id"]): p for p in db_positions
        }
        onchain_map = {
            (p["condition_id"], p["token_id"]): p for p in onchain_positions
        }

        result = {"added": [], "removed": [], "updated": []}

        # Zincirde var, DB'de yok -> Ekle
        for key, pos in onchain_map.items():
            if key not in db_map:
                self.upsert_position(
                    {
                        "condition_id": key[0],
                        "token_id": key[1],
                        "size": pos.get("size", 0),
                        "entry_price": pos.get("avg_price", 0),
                        "is_open": True,
                        "source": "onchain_discovery",
                    }
                )
                result["added"].append(key)
                logger.info(f"Yeni pozisyon kesfedildi: {key}")

        # DB'de var, zincirde yok -> Kapat
        for key, pos in db_map.items():
            if key not in onchain_map:
                self.close_position(key[0], key[1])
                result["removed"].append(key)
                logger.info(f"Pozisyon kapandi/likide edildi: {key}")

        # Her ikisinde de var -> Boyut farki varsa guncelle
        for key in db_map.keys() & onchain_map.keys():
            db_size = db_map[key].get("size", 0)
            chain_size = onchain_map[key].get("size", 0)
            if abs(db_size - chain_size) > 0.001:
                self.upsert_position(
                    {
                        "condition_id": key[0],
                        "token_id": key[1],
                        "size": chain_size,
                        "is_open": chain_size > 0,
                    }
                )
                result["updated"].append(key)
                logger.info(
                    f"Pozisyon boyutu guncellendi: {key} "
                    f"({db_size} -> {chain_size})"
                )

        return result
