"""
Türkiye Özel Ders Piyasası - Analiz ve Raporlama
=================================================
Bu script veritabanındaki verileri analiz eder ve
dashboard/raporlar için hazırlar.

Kullanım:
    python analytics.py --report weekly
    python analytics.py --report monthly
    python analytics.py --export csv
"""

import os
import json
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from dataclasses import dataclass

import pandas as pd
import numpy as np
from supabase import create_client, Client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# =====================================================
# CONFIGURATION
# =====================================================

SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')

# =====================================================
# DATA MODELS
# =====================================================

@dataclass
class MarketSummary:
    """Piyasa özet istatistikleri"""
    total_listings: int
    active_listings: int
    avg_price: float
    median_price: float
    price_change_percent: float  # Son 2 haftaya göre
    top_categories: List[Dict]
    top_provinces: List[Dict]
    generated_at: datetime

# =====================================================
# ANALYTICS CLASS
# =====================================================

class TutorMarketAnalytics:
    """Özel ders piyasası analiz sınıfı"""
    
    def __init__(self):
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise ValueError("Supabase credentials not set")
        self.client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    # ----------------
    # DATA FETCHING
    # ----------------
    
    def get_all_listings(self, active_only: bool = True) -> pd.DataFrame:
        """Tüm ilanları DataFrame olarak getir"""
        query = self.client.table('listings').select(
            '*',
            'lesson_categories(name, slug)',
            'provinces(name, region)',
            'districts(name)'
        )
        
        if active_only:
            query = query.eq('is_active', True)
        
        result = query.execute()
        
        if not result.data:
            return pd.DataFrame()
        
        df = pd.DataFrame(result.data)
        
        # Flatten nested data
        if 'lesson_categories' in df.columns:
            df['category_name'] = df['lesson_categories'].apply(
                lambda x: x['name'] if x else None
            )
        if 'provinces' in df.columns:
            df['province_name'] = df['provinces'].apply(
                lambda x: x['name'] if x else None
            )
            df['region'] = df['provinces'].apply(
                lambda x: x['region'] if x else None
            )
        
        return df
    
    def get_price_history(self, days: int = 90) -> pd.DataFrame:
        """Son N günün fiyat geçmişini getir"""
        since = (datetime.now() - timedelta(days=days)).isoformat()
        
        result = self.client.table('price_history').select(
            '*',
            'listings(category_id, province_id)'
        ).gte('recorded_at', since).execute()
        
        if not result.data:
            return pd.DataFrame()
        
        return pd.DataFrame(result.data)
    
    def get_materialized_view(self, view_name: str) -> pd.DataFrame:
        """Materialized view'dan veri getir"""
        result = self.client.table(view_name).select('*').execute()
        
        if not result.data:
            return pd.DataFrame()
        
        return pd.DataFrame(result.data)
    
    # ----------------
    # ANALYSIS
    # ----------------
    
    def calculate_market_summary(self) -> MarketSummary:
        """Piyasa özet istatistiklerini hesapla"""
        df = self.get_all_listings()
        
        if df.empty:
            return MarketSummary(
                total_listings=0,
                active_listings=0,
                avg_price=0,
                median_price=0,
                price_change_percent=0,
                top_categories=[],
                top_provinces=[],
                generated_at=datetime.now()
            )
        
        # Temel istatistikler
        total = len(df)
        active = len(df[df['is_active'] == True])
        
        prices = df['price_per_hour'].dropna()
        avg_price = prices.mean() if len(prices) > 0 else 0
        median_price = prices.median() if len(prices) > 0 else 0
        
        # Fiyat değişimi (son 2 hafta)
        price_change = self._calculate_price_change(14)
        
        # Top kategoriler
        top_cats = df.groupby('category_name').agg({
            'id': 'count',
            'price_per_hour': 'mean'
        }).reset_index()
        top_cats.columns = ['category', 'count', 'avg_price']
        top_cats = top_cats.nlargest(10, 'count').to_dict('records')
        
        # Top şehirler
        top_provs = df.groupby('province_name').agg({
            'id': 'count',
            'price_per_hour': 'mean'
        }).reset_index()
        top_provs.columns = ['province', 'count', 'avg_price']
        top_provs = top_provs.nlargest(10, 'count').to_dict('records')
        
        return MarketSummary(
            total_listings=total,
            active_listings=active,
            avg_price=round(avg_price, 2),
            median_price=round(median_price, 2),
            price_change_percent=round(price_change, 2),
            top_categories=top_cats,
            top_provinces=top_provs,
            generated_at=datetime.now()
        )
    
    def _calculate_price_change(self, days: int) -> float:
        """Son N gündeki fiyat değişimini hesapla"""
        history = self.get_price_history(days * 2)
        
        if history.empty:
            return 0.0
        
        history['recorded_at'] = pd.to_datetime(history['recorded_at'])
        
        cutoff = datetime.now() - timedelta(days=days)
        
        recent = history[history['recorded_at'] >= cutoff]['price'].mean()
        older = history[history['recorded_at'] < cutoff]['price'].mean()
        
        if older and older > 0:
            return ((recent - older) / older) * 100
        
        return 0.0
    
    def analyze_by_category(self) -> pd.DataFrame:
        """Kategori bazlı detaylı analiz"""
        df = self.get_materialized_view('mv_category_price_stats')
        
        if df.empty:
            return df
        
        # Fiyat endeksi hesapla (Türkiye ortalamasına göre)
        overall_avg = df['avg_price'].mean()
        df['price_index'] = (df['avg_price'] / overall_avg * 100).round(1)
        
        # Coefficient of variation (volatilite)
        df['cv'] = (df['std_dev'] / df['avg_price'] * 100).round(1)
        
        return df.sort_values('listing_count', ascending=False)
    
    def analyze_by_province(self) -> pd.DataFrame:
        """Şehir bazlı detaylı analiz"""
        df = self.get_materialized_view('mv_province_price_stats')
        
        if df.empty:
            return df
        
        # Fiyat endeksi
        overall_avg = df['avg_price'].mean()
        df['price_index'] = (df['avg_price'] / overall_avg * 100).round(1)
        
        return df.sort_values('listing_count', ascending=False)
    
    def analyze_trends(self, weeks: int = 12) -> pd.DataFrame:
        """Haftalık trend analizi"""
        df = self.get_materialized_view('mv_weekly_trends')
        
        if df.empty:
            return df
        
        df['week_start'] = pd.to_datetime(df['week_start'])
        
        # Son N hafta
        cutoff = datetime.now() - timedelta(weeks=weeks)
        df = df[df['week_start'] >= cutoff]
        
        return df.sort_values('week_start')
    
    def compare_categories(self, category_ids: List[int]) -> pd.DataFrame:
        """Kategorileri karşılaştır"""
        df = self.analyze_by_category()
        return df[df['category_id'].isin(category_ids)]
    
    def compare_provinces(self, province_ids: List[int]) -> pd.DataFrame:
        """Şehirleri karşılaştır"""
        df = self.analyze_by_province()
        return df[df['province_id'].isin(province_ids)]
    
    # ----------------
    # SEASONAL ANALYSIS
    # ----------------
    
    def get_seasonal_patterns(self) -> Dict[str, Any]:
        """Sezonsal pattern'leri analiz et"""
        history = self.get_price_history(365)  # 1 yıl
        
        if history.empty:
            return {}
        
        history['recorded_at'] = pd.to_datetime(history['recorded_at'])
        history['month'] = history['recorded_at'].dt.month
        history['week'] = history['recorded_at'].dt.isocalendar().week
        
        # Aylık ortalamalar
        monthly = history.groupby('month')['price'].agg(['mean', 'count']).reset_index()
        monthly.columns = ['month', 'avg_price', 'listing_count']
        
        # Önemli dönemler
        # YKS: Haziran, LGS: Haziran, Okul başlangıcı: Eylül
        seasonal_events = {
            'yks_lgs_peak': {
                'months': [5, 6],
                'description': 'YKS ve LGS sınav dönemi'
            },
            'school_start': {
                'months': [9, 10],
                'description': 'Okul başlangıcı'
            },
            'midterm': {
                'months': [1, 2],
                'description': 'Yarıyıl sınavları'
            },
            'summer_low': {
                'months': [7, 8],
                'description': 'Yaz tatili (düşük talep)'
            }
        }
        
        return {
            'monthly_averages': monthly.to_dict('records'),
            'seasonal_events': seasonal_events,
            'analysis_period_days': 365
        }
    
    # ----------------
    # EXPORT
    # ----------------
    
    def export_to_csv(self, output_dir: str = './exports'):
        """Tüm verileri CSV olarak dışa aktar"""
        import os
        os.makedirs(output_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Listings
        df_listings = self.get_all_listings(active_only=False)
        if not df_listings.empty:
            df_listings.to_csv(
                f'{output_dir}/listings_{timestamp}.csv',
                index=False,
                encoding='utf-8-sig'
            )
        
        # Category stats
        df_cats = self.analyze_by_category()
        if not df_cats.empty:
            df_cats.to_csv(
                f'{output_dir}/category_stats_{timestamp}.csv',
                index=False,
                encoding='utf-8-sig'
            )
        
        # Province stats
        df_provs = self.analyze_by_province()
        if not df_provs.empty:
            df_provs.to_csv(
                f'{output_dir}/province_stats_{timestamp}.csv',
                index=False,
                encoding='utf-8-sig'
            )
        
        # Trends
        df_trends = self.analyze_trends()
        if not df_trends.empty:
            df_trends.to_csv(
                f'{output_dir}/weekly_trends_{timestamp}.csv',
                index=False,
                encoding='utf-8-sig'
            )
        
        print(f"Data exported to {output_dir}/")
    
    def export_to_json(self, output_file: str = './exports/market_data.json'):
        """Tüm verileri JSON olarak dışa aktar"""
        import os
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        
        summary = self.calculate_market_summary()
        
        data = {
            'generated_at': datetime.now().isoformat(),
            'summary': {
                'total_listings': summary.total_listings,
                'active_listings': summary.active_listings,
                'avg_price': summary.avg_price,
                'median_price': summary.median_price,
                'price_change_percent': summary.price_change_percent,
            },
            'top_categories': summary.top_categories,
            'top_provinces': summary.top_provinces,
            'seasonal_patterns': self.get_seasonal_patterns()
        }
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        
        print(f"Data exported to {output_file}")

# =====================================================
# REPORT GENERATOR
# =====================================================

class ReportGenerator:
    """Rapor oluşturucu"""
    
    def __init__(self, analytics: TutorMarketAnalytics):
        self.analytics = analytics
    
    def generate_weekly_report(self) -> str:
        """Haftalık özet raporu oluştur"""
        summary = self.analytics.calculate_market_summary()
        
        report = f"""
╔══════════════════════════════════════════════════════════════╗
║          TÜRKİYE ÖZEL DERS PİYASASI - HAFTALIK RAPOR          ║
║                    {datetime.now().strftime('%d.%m.%Y')}                           ║
╚══════════════════════════════════════════════════════════════╝

📊 GENEL İSTATİSTİKLER
─────────────────────────────────────────────────────────────────
  Toplam İlan Sayısı     : {summary.total_listings:,}
  Aktif İlan Sayısı      : {summary.active_listings:,}
  Ortalama Ücret         : {summary.avg_price:,.0f} TL/saat
  Medyan Ücret           : {summary.median_price:,.0f} TL/saat
  2 Haftalık Değişim     : {summary.price_change_percent:+.1f}%

📚 EN POPÜLER DERSLER (İlan Sayısına Göre)
─────────────────────────────────────────────────────────────────
"""
        for i, cat in enumerate(summary.top_categories[:5], 1):
            report += f"  {i}. {cat['category']:<20} {cat['count']:>5} ilan  │  Ort: {cat['avg_price']:,.0f} TL\n"
        
        report += """
🏙️ EN AKTİF ŞEHİRLER (İlan Sayısına Göre)
─────────────────────────────────────────────────────────────────
"""
        for i, prov in enumerate(summary.top_provinces[:5], 1):
            report += f"  {i}. {prov['province']:<20} {prov['count']:>5} ilan  │  Ort: {prov['avg_price']:,.0f} TL\n"
        
        report += f"""
─────────────────────────────────────────────────────────────────
Rapor oluşturma zamanı: {summary.generated_at.strftime('%d.%m.%Y %H:%M')}
"""
        
        return report
    
    def generate_monthly_report(self) -> str:
        """Aylık detaylı rapor oluştur"""
        summary = self.analytics.calculate_market_summary()
        cat_analysis = self.analytics.analyze_by_category()
        prov_analysis = self.analytics.analyze_by_province()
        seasonal = self.analytics.get_seasonal_patterns()
        
        report = f"""
╔══════════════════════════════════════════════════════════════╗
║          TÜRKİYE ÖZEL DERS PİYASASI - AYLIK RAPOR            ║
║                    {datetime.now().strftime('%B %Y')}                            ║
╚══════════════════════════════════════════════════════════════╝

═══════════════════════════════════════════════════════════════
                         ÖZET İSTATİSTİKLER
═══════════════════════════════════════════════════════════════

  📈 Toplam İlan          : {summary.total_listings:,}
  ✅ Aktif İlan           : {summary.active_listings:,}
  💰 Ortalama Ücret       : {summary.avg_price:,.0f} TL/saat
  📊 Medyan Ücret         : {summary.median_price:,.0f} TL/saat
  📉 Aylık Değişim        : {summary.price_change_percent:+.1f}%

═══════════════════════════════════════════════════════════════
                      KATEGORİ ANALİZİ (İLK 15)
═══════════════════════════════════════════════════════════════

{'Kategori':<25} {'İlan':>7} {'Ort.TL':>8} {'Med.TL':>8} {'Endeks':>7}
───────────────────────────────────────────────────────────────
"""
        
        if not cat_analysis.empty:
            for _, row in cat_analysis.head(15).iterrows():
                name = str(row.get('category_name', 'N/A'))[:24]
                report += f"{name:<25} {int(row.get('listing_count', 0)):>7} {row.get('avg_price', 0):>8.0f} {row.get('median_price', 0):>8.0f} {row.get('price_index', 0):>6.0f}%\n"
        
        report += f"""
═══════════════════════════════════════════════════════════════
                       ŞEHİR ANALİZİ (İLK 15)
═══════════════════════════════════════════════════════════════

{'Şehir':<20} {'Bölge':<15} {'İlan':>7} {'Ort.TL':>8} {'Endeks':>7}
───────────────────────────────────────────────────────────────
"""
        
        if not prov_analysis.empty:
            for _, row in prov_analysis.head(15).iterrows():
                name = str(row.get('province_name', 'N/A'))[:19]
                region = str(row.get('region', 'N/A'))[:14]
                report += f"{name:<20} {region:<15} {int(row.get('listing_count', 0)):>7} {row.get('avg_price', 0):>8.0f} {row.get('price_index', 0):>6.0f}%\n"
        
        report += f"""
═══════════════════════════════════════════════════════════════
                      SEZONSAL PATTERN'LER
═══════════════════════════════════════════════════════════════

  🎓 Sınav Dönemi (Mayıs-Haziran)    : Yüksek talep, fiyat artışı beklenir
  📚 Okul Başlangıcı (Eylül-Ekim)    : Yüksek talep
  📝 Yarıyıl Sınavları (Ocak-Şubat)  : Orta-yüksek talep
  🏖️ Yaz Tatili (Temmuz-Ağustos)     : Düşük talep, fiyat düşüşü beklenir

───────────────────────────────────────────────────────────────
Rapor oluşturma zamanı: {datetime.now().strftime('%d.%m.%Y %H:%M')}
═══════════════════════════════════════════════════════════════
"""
        
        return report

# =====================================================
# MAIN
# =====================================================

def main():
    parser = argparse.ArgumentParser(description='Özel Ders Piyasası Analiz')
    parser.add_argument('--report', choices=['weekly', 'monthly'],
                        help='Rapor türü')
    parser.add_argument('--export', choices=['csv', 'json'],
                        help='Dışa aktarma formatı')
    parser.add_argument('--output', default='./exports',
                        help='Çıktı dizini')
    
    args = parser.parse_args()
    
    analytics = TutorMarketAnalytics()
    
    if args.report:
        reporter = ReportGenerator(analytics)
        if args.report == 'weekly':
            print(reporter.generate_weekly_report())
        elif args.report == 'monthly':
            print(reporter.generate_monthly_report())
    
    elif args.export:
        if args.export == 'csv':
            analytics.export_to_csv(args.output)
        elif args.export == 'json':
            analytics.export_to_json(f'{args.output}/market_data.json')
    
    else:
        # Default: summary
        summary = analytics.calculate_market_summary()
        print(f"\n📊 Piyasa Özeti")
        print(f"   Toplam İlan: {summary.total_listings:,}")
        print(f"   Ortalama Ücret: {summary.avg_price:,.0f} TL/saat")
        print(f"   Son 2 Hafta Değişim: {summary.price_change_percent:+.1f}%\n")

if __name__ == '__main__':
    main()
