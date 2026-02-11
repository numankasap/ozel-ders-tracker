"""
LLM Analiz Ajani - Ajanin Beyni

RAG (Retrieval-Augmented Generation) mimarisi ile:
1. Piyasa sorusunu arama sorgularina donusturur
2. Guncel haberleri toplar (Tavily / SerpApi)
3. LLM (GPT-4o / Claude) ile Bayesci olasilik tahmini yapar
4. Chain-of-Thought (CoT) ile super tahmincilik

Philip Tetlock tarzi kalibrasyon hedeflenir.
"""

import os
import re
import json
import logging
from typing import Optional

import requests
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# ─── Sabitler ────────────────────────────────────────────────────

SUPERFORECASTER_SYSTEM_PROMPT = """Sen Philip Tetlock tarzinda bir super tahmincisin.
Gorevlerin:
- Haberleri ve kanitleri nesnel olarak degerlendirmek
- Bayesci guncelleme yaparak olasilik hesaplamak
- Asiri guven (overconfidence) ve capa etkisinden (anchoring) kacinmak
- Her zaman %0 ile %100 arasinda kesin bir sayi vermek

ONEMLI KURALLAR:
1. Piyasa fiyatindan BAGMSIZ dusun. Piyasa yanlis olabilir.
2. Temel orani (base rate) her zaman dikkate al.
3. Son haberlere asiri tepki verme (recency bias).
4. Belirsizlik varsa %50'ye yakin kal.
5. Cevabini her zaman PROBABILITY: X.XX formatiyla bitir."""

ANALYSIS_PROMPT_TEMPLATE = """
PIYASA SORUSU: {question}

PIYASANIN MEVCUT FIYATI (Ima Edilen Olasilik): %{market_prob:.1f}

GUNCEL HABERLER VE KANITLAR:
{news_context}

ANALIZ ADIMLARI:
1. TEMEL ORAN: Bu tip olaylarin tarihsel olarak gerceklesme orani nedir?
2. KANITLAR: Her bir haber/kanit, olasiligi NASIL etkiliyor?
   - Destekleyen kanitlar (olasiligi artiran)
   - Karsi kanitlar (olasiligi azaltan)
3. KARSI ARGUMAN (Devil's Advocate): Bu olayIN GERCEKLESMEMESI icin en guclu
   arguman nedir?
4. SIYAH KUGU: Dusuk olasilikli ama yuksek etkili riskler var mi?
5. SONUC: Tum kanitleri tarttiktan sonra, bu olayIN gerceklesme olasligini
   %0 ile %100 arasinda ver.

Cevabini su formatla bitir:
PROBABILITY: 0.XX
"""

SEARCH_QUERY_PROMPT = """Asagidaki tahmin piyasasi sorusu icin 3 adet arama motoru
sorgusu olustur. Sorgular Ingilizce ve guncel haberleri bulmaya yonelik olmali.

Piyasa Sorusu: {question}

Sorgulari su formatta ver:
QUERY1: ...
QUERY2: ...
QUERY3: ...
"""


class AnalystAgent:
    """
    LLM tabanli piyasa analiz ajani.

    Sorumluluklar:
    - Piyasa sorularini arama sorgularina donusturme
    - Haber toplama (news fetching)
    - LLM ile olasilik tahmini
    - Guven skoru ve gerekce uretme
    """

    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError("OPENAI_API_KEY ortam degiskeni tanimlanmali.")

        self.llm = OpenAI(api_key=api_key)
        self.model = os.getenv("LLM_MODEL", "gpt-4o")
        self.news_api_key = os.getenv("TAVILY_API_KEY", "")
        self.serp_api_key = os.getenv("SERP_API_KEY", "")

        logger.info(f"Analyst Agent baslatildi (model={self.model})")

    # ─── Haber Toplama ──────────────────────────────────────────

    def _generate_search_queries(self, question: str) -> list[str]:
        """Piyasa sorusundan arama sorgulari uretir."""
        try:
            response = self.llm.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "user", "content": SEARCH_QUERY_PROMPT.format(
                        question=question
                    )},
                ],
                temperature=0.3,
                max_tokens=200,
            )
            text = response.choices[0].message.content or ""
            queries = []
            for line in text.strip().split("\n"):
                line = line.strip()
                if line.startswith("QUERY"):
                    query = line.split(":", 1)[-1].strip()
                    if query:
                        queries.append(query)
            return queries[:3] if queries else [question]
        except Exception as e:
            logger.warning(f"Sorgu uretme hatasi: {e}")
            return [question]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def _fetch_news_tavily(self, query: str) -> list[dict]:
        """Tavily API ile haber arar."""
        if not self.news_api_key:
            return []
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self.news_api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": 5,
                    "include_answer": False,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return [
                {
                    "title": r.get("title", ""),
                    "content": r.get("content", "")[:500],
                    "url": r.get("url", ""),
                    "published_date": r.get("published_date", ""),
                }
                for r in data.get("results", [])
            ]
        except Exception as e:
            logger.warning(f"Tavily haber cekme hatasi: {e}")
            return []

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def _fetch_news_serp(self, query: str) -> list[dict]:
        """SerpApi ile haber arar (Tavily yedegi)."""
        if not self.serp_api_key:
            return []
        try:
            resp = requests.get(
                "https://serpapi.com/search",
                params={
                    "api_key": self.serp_api_key,
                    "q": query,
                    "tbm": "nws",
                    "num": 5,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return [
                {
                    "title": r.get("title", ""),
                    "content": r.get("snippet", "")[:500],
                    "url": r.get("link", ""),
                    "published_date": r.get("date", ""),
                }
                for r in data.get("news_results", [])
            ]
        except Exception as e:
            logger.warning(f"SerpApi haber cekme hatasi: {e}")
            return []

    def gather_news(self, question: str) -> str:
        """
        Piyasa sorusu icin guncel haberleri toplar.
        Oncelik: Tavily > SerpApi > Bos
        """
        queries = self._generate_search_queries(question)
        all_news = []

        for query in queries:
            # Tavily'i dene
            news = self._fetch_news_tavily(query)
            if not news:
                # Yedek: SerpApi
                news = self._fetch_news_serp(query)
            all_news.extend(news)

        if not all_news:
            return "Guncel haber bulunamadi. Analizi mevcut bilgilerle yap."

        # Tekrarlari kaldir (basliga gore)
        seen_titles = set()
        unique_news = []
        for n in all_news:
            title_key = n["title"].lower().strip()
            if title_key not in seen_titles:
                seen_titles.add(title_key)
                unique_news.append(n)

        # Formatlama
        context_parts = []
        for i, n in enumerate(unique_news[:10], 1):
            date_str = f" ({n['published_date']})" if n.get("published_date") else ""
            context_parts.append(
                f"[{i}] {n['title']}{date_str}\n    {n['content']}"
            )

        return "\n\n".join(context_parts)

    # ─── LLM Analiz ─────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """LLM'i cagirir ve yaniti dondurur."""
        response = self.llm.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=1500,
        )
        return response.choices[0].message.content or ""

    def _extract_probability(self, text: str) -> Optional[float]:
        """LLM yanitindan olasilik degerini cikarir."""
        # PROBABILITY: 0.XX formatini ara
        patterns = [
            r"PROBABILITY:\s*([0-9]*\.?[0-9]+)",
            r"probability:\s*([0-9]*\.?[0-9]+)",
            r"Olasilik:\s*%?([0-9]*\.?[0-9]+)",
            r"(\d+\.?\d*)%",  # Yuzde formati
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = float(match.group(1))
                # Yuzde olarak gelmis olabilir
                if value > 1.0:
                    value = value / 100.0
                # Sinir kontrolu
                return max(0.01, min(0.99, value))

        logger.warning("LLM yanitindan olasilik cikarilamadi.")
        return None

    def analyze_market(
        self,
        question: str,
        market_price: float,
        description: str = "",
    ) -> dict:
        """
        Bir piyasayi analiz eder ve olasilik tahmini yapar.

        Args:
            question: Piyasa sorusu
            market_price: Mevcut piyasa fiyati (0-1)
            description: Ek piyasa aciklamasi

        Returns:
            {
                "ai_probability": float,
                "rationale": str,
                "confidence": str,  # "low", "medium", "high"
                "edge": float,
                "news_summary": str
            }
        """
        logger.info(f"Piyasa analiz ediliyor: {question[:80]}...")

        # 1. Haber topla
        news_context = self.gather_news(question)

        # 2. Ek baglam varsa ekle
        if description:
            news_context = f"PIYASA ACIKLAMASI:\n{description}\n\n{news_context}"

        # 3. LLM'e sor
        user_prompt = ANALYSIS_PROMPT_TEMPLATE.format(
            question=question,
            market_prob=market_price * 100,
            news_context=news_context,
        )

        llm_response = self._call_llm(SUPERFORECASTER_SYSTEM_PROMPT, user_prompt)

        # 4. Olasiligi cikar
        ai_prob = self._extract_probability(llm_response)

        if ai_prob is None:
            logger.warning("Olasilik cikarilamadi, varsayilan %50 kullaniliyor.")
            ai_prob = 0.50

        # 5. Kenar (Edge) hesapla
        edge = ai_prob - market_price

        # 6. Guven seviyesi belirle
        if abs(edge) > 0.20:
            confidence = "high"
        elif abs(edge) > 0.10:
            confidence = "medium"
        else:
            confidence = "low"

        result = {
            "ai_probability": round(ai_prob, 4),
            "rationale": llm_response[:2000],
            "confidence": confidence,
            "edge": round(edge, 4),
            "news_summary": news_context[:1000],
        }

        logger.info(
            f"Analiz tamamlandi: AI={ai_prob:.2%}, "
            f"Piyasa={market_price:.2%}, Edge={edge:+.2%}, "
            f"Guven={confidence}"
        )

        return result

    def generate_trade_rationale(
        self, question: str, side: str, ai_prob: float, market_price: float
    ) -> str:
        """Islem gerekce metni uretir (trade_logs icin)."""
        prompt = (
            f"Kisa bir islem gerekce ozeti yaz (3 cumle):\n"
            f"Piyasa: {question}\n"
            f"Islem Yonu: {side}\n"
            f"AI Olasiligi: {ai_prob:.2%}\n"
            f"Piyasa Fiyati: {market_price:.2%}\n"
            f"Kenar (Edge): {(ai_prob - market_price):+.2%}"
        )
        try:
            response = self._call_llm(
                "Kisa ve ozetleyici islem gerekceleri yazan bir finansal asistansin.",
                prompt,
            )
            return response[:500]
        except Exception:
            return f"{side} - AI: {ai_prob:.2%} vs Market: {market_price:.2%}"
