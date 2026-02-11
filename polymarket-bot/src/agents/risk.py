"""
Risk Yonetimi Modulu - Ajanin Hayatta Kalma Icgudusu

Kelly Kriteri tabanli pozisyon boyutlandirma, sermaye koruma
protokolu ve finansal guvenlik duvarlari.

Temel Felsefe:
- "Ne kadar" oynayacagini bilmek, "neye" oynayacagini bilmekten
  daha onemlidir.
- Agresiflik yerine hayatta kalma onceliklenir.
- Tam Kelly cok volatildir -> Kesirli Kelly (1/4) kullanilir.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RiskAssessment:
    """Risk degerlendirme sonucu."""

    should_trade: bool
    order_size_usdc: float
    kelly_fraction: float
    kelly_raw: float
    reason: str
    risk_level: str  # "normal", "cautious", "emergency", "blocked"


class RiskManager:
    """
    Portfoy risk yonetimi ve pozisyon boyutlandirma.

    Kurallar:
    1. Stop-Out: Portfoy, baslangicin %20'sinin altina duserse Acil Durum Modu.
    2. Max Pozisyon: Tek piyasaya portfoyun %20'sinden fazlasi yatirilmaz.
    3. Max Emir: Tek emir $50'i asamaz (hard cap).
    4. Min Kenar (Edge): AI-Piyasa farki %10'dan az ise islem yapilmaz.
    5. Kesirli Kelly: Tam Kelly yerine 1/4 Kelly kullanilir.
    """

    def __init__(self, params: dict):
        self.kelly_fraction = params.get("kelly_fraction", 0.25)
        self.max_position_pct = params.get("max_position_pct", 0.20)
        self.max_single_order = params.get("max_single_order_usdc", 50.0)
        self.stop_out_pct = params.get("stop_out_pct", 0.20)
        self.min_edge = params.get("min_edge", 0.10)
        self.emergency_threshold = params.get("emergency_threshold_pct", 0.20)
        self.emergency_min_confidence = params.get(
            "emergency_min_confidence", 0.90
        )

        logger.info(
            f"RiskManager baslatildi: kelly_frac={self.kelly_fraction}, "
            f"max_pos={self.max_position_pct:.0%}, "
            f"max_order=${self.max_single_order}"
        )

    def calculate_kelly(
        self, ai_probability: float, market_price: float
    ) -> float:
        """
        Kelly Kriteri ile optimal bahis oranini hesaplar.

        f* = (bp - q) / b
        b = (1 / price) - 1  (net oran)
        p = AI olasiligi
        q = 1 - p

        Returns:
            Portfoyun riske atilacak yuzde (0-1). Negatif = islem yapma.
        """
        if market_price <= 0.01 or market_price >= 0.99:
            return 0.0

        b = (1.0 / market_price) - 1.0  # Net oran
        p = ai_probability
        q = 1.0 - p

        if b <= 0:
            return 0.0

        kelly_raw = (b * p - q) / b

        # Negatif Kelly = Edge yok, islem yapma
        if kelly_raw <= 0:
            return 0.0

        # Kesirli Kelly uygula
        kelly_adjusted = kelly_raw * self.kelly_fraction

        # Asla portfoyun %25'inden fazlasini riske atma
        kelly_adjusted = min(kelly_adjusted, 0.25)

        return kelly_adjusted

    def assess_trade(
        self,
        ai_probability: float,
        market_price: float,
        current_balance: float,
        initial_balance: float,
        existing_position_size: float = 0.0,
        side: str = "BUY",
    ) -> RiskAssessment:
        """
        Bir ticaret firsatini risk acisindan degerlendirir.

        Args:
            ai_probability: LLM'in hesapladigi gerceklesme olasiligi (0-1)
            market_price: Mevcut piyasa fiyati (0-1)
            current_balance: Mevcut USDC bakiyesi
            initial_balance: Baslangic sermayesi
            existing_position_size: Bu piyasadaki mevcut pozisyon buyuklugu
            side: "BUY" veya "SELL"

        Returns:
            RiskAssessment
        """

        # ─── 1. Bakiye Kontrolu ─────────────────────────────────
        if current_balance <= 0:
            return RiskAssessment(
                should_trade=False,
                order_size_usdc=0,
                kelly_fraction=0,
                kelly_raw=0,
                reason="Bakiye sifir - Ajan oldu.",
                risk_level="blocked",
            )

        # ─── 2. Stop-Out Kontrolu ───────────────────────────────
        if initial_balance > 0:
            drawdown_pct = current_balance / initial_balance
            if drawdown_pct < self.stop_out_pct:
                return RiskAssessment(
                    should_trade=False,
                    order_size_usdc=0,
                    kelly_fraction=0,
                    kelly_raw=0,
                    reason=(
                        f"STOP-OUT: Bakiye baslangicin "
                        f"%{drawdown_pct*100:.0f}'ine dustu. "
                        f"Ajan kis uykusunda."
                    ),
                    risk_level="blocked",
                )

        # ─── 3. Acil Durum Modu Kontrolu ────────────────────────
        is_emergency = False
        if initial_balance > 0:
            balance_ratio = current_balance / initial_balance
            if balance_ratio < self.emergency_threshold:
                is_emergency = True
                if ai_probability < self.emergency_min_confidence:
                    return RiskAssessment(
                        should_trade=False,
                        order_size_usdc=0,
                        kelly_fraction=0,
                        kelly_raw=0,
                        reason=(
                            f"ACIL DURUM MODU: Bakiye %{balance_ratio*100:.0f}, "
                            f"guven {ai_probability:.0%} < "
                            f"{self.emergency_min_confidence:.0%} esigi."
                        ),
                        risk_level="emergency",
                    )

        # ─── 4. Kenar (Edge) Kontrolu ───────────────────────────
        if side == "BUY":
            edge = ai_probability - market_price
        else:
            edge = market_price - ai_probability

        if edge < self.min_edge:
            return RiskAssessment(
                should_trade=False,
                order_size_usdc=0,
                kelly_fraction=0,
                kelly_raw=0,
                reason=(
                    f"Yetersiz kenar (edge): {edge:+.2%} < "
                    f"{self.min_edge:.2%} esigi."
                ),
                risk_level="normal",
            )

        # ─── 5. Kelly Hesapla ────────────────────────────────────
        if side == "BUY":
            kelly_raw_val = self.calculate_kelly(ai_probability, market_price)
        else:
            kelly_raw_val = self.calculate_kelly(
                1.0 - ai_probability, 1.0 - market_price
            )

        if kelly_raw_val <= 0:
            return RiskAssessment(
                should_trade=False,
                order_size_usdc=0,
                kelly_fraction=kelly_raw_val,
                kelly_raw=kelly_raw_val / self.kelly_fraction
                if self.kelly_fraction > 0
                else 0,
                reason="Kelly negatif - Beklenen deger negatif.",
                risk_level="normal",
            )

        # ─── 6. Pozisyon Buyuklugu Hesapla ──────────────────────
        order_size = current_balance * kelly_raw_val

        # Acil Durum Modunda ek kisitlama
        if is_emergency:
            order_size = min(order_size, current_balance * 0.05)

        # Max pozisyon kontrolu
        max_position = current_balance * self.max_position_pct
        total_exposure = existing_position_size + order_size
        if total_exposure > max_position:
            order_size = max(0, max_position - existing_position_size)

        # Hard cap
        order_size = min(order_size, self.max_single_order)

        # Minimum islem buyuklugu ($1)
        if order_size < 1.0:
            return RiskAssessment(
                should_trade=False,
                order_size_usdc=0,
                kelly_fraction=kelly_raw_val,
                kelly_raw=kelly_raw_val / self.kelly_fraction
                if self.kelly_fraction > 0
                else 0,
                reason=f"Islem buyuklugu cok kucuk: ${order_size:.2f} < $1.00",
                risk_level="cautious" if is_emergency else "normal",
            )

        # ─── 7. Sonuc ───────────────────────────────────────────
        risk_level = "emergency" if is_emergency else "normal"

        return RiskAssessment(
            should_trade=True,
            order_size_usdc=round(order_size, 2),
            kelly_fraction=round(kelly_raw_val, 4),
            kelly_raw=round(
                kelly_raw_val / self.kelly_fraction
                if self.kelly_fraction > 0
                else 0,
                4,
            ),
            reason=(
                f"ISLEM ONAYLI: Edge={edge:+.2%}, "
                f"Kelly={kelly_raw_val:.2%}, "
                f"Boyut=${order_size:.2f}"
            ),
            risk_level=risk_level,
        )

    def calculate_expected_value(
        self, ai_probability: float, market_price: float, size: float
    ) -> float:
        """
        Beklenen degeri (EV) hesaplar.

        EV = (p * kazanc) - (q * kayip)
        """
        p = ai_probability
        q = 1.0 - p
        potential_profit = size * ((1.0 / market_price) - 1.0)
        potential_loss = size

        ev = (p * potential_profit) - (q * potential_loss)
        return round(ev, 4)

    def portfolio_health_check(
        self, current_balance: float, initial_balance: float, open_positions: list
    ) -> dict:
        """
        Portfoy saglik kontrolu yapar.

        Returns:
            {
                "status": "healthy" | "warning" | "critical" | "dead",
                "balance_ratio": float,
                "total_exposure": float,
                "open_position_count": int,
                "recommendation": str
            }
        """
        if current_balance <= 0:
            return {
                "status": "dead",
                "balance_ratio": 0,
                "total_exposure": 0,
                "open_position_count": 0,
                "recommendation": "Ajan oldu. Sermaye tukendi.",
            }

        balance_ratio = (
            current_balance / initial_balance if initial_balance > 0 else 1.0
        )
        total_exposure = sum(
            p.get("size", 0) * p.get("entry_price", 0) for p in open_positions
        )

        if balance_ratio < self.stop_out_pct:
            status = "critical"
            rec = "KRITIK: Stop-out seviyesinin altinda. Tum islemler durduruldu."
        elif balance_ratio < self.emergency_threshold:
            status = "warning"
            rec = (
                "UYARI: Acil durum modunda. Sadece yuksek guvenli "
                "islemlere izin veriliyor."
            )
        elif balance_ratio < 0.50:
            status = "warning"
            rec = (
                "UYARI: Onemli sermaye kaybi. Muhafazakar strateji oneriliyor."
            )
        else:
            status = "healthy"
            rec = "Portfoy saglikli. Normal islem parametreleri aktif."

        return {
            "status": status,
            "balance_ratio": round(balance_ratio, 4),
            "total_exposure": round(total_exposure, 2),
            "open_position_count": len(open_positions),
            "recommendation": rec,
        }
