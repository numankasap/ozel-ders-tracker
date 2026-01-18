# 🎓 Türkiye Özel Ders Piyasası Tracker

Türkiye'deki özel ders ilanlarını sistematik olarak takip eden, fiyat trendlerini analiz eden ve piyasa istatistikleri üreten bir veri toplama sistemi.

## 📋 Özellikler

- **Otomatik Veri Toplama**: 2 haftada bir ozelders.com'dan ilan verisi çeker
- **KVKK Uyumlu**: Sadece anonim veriler toplanır (fiyat, konum, ders türü)
- **Trend Analizi**: Fiyat değişimlerini zaman serisi olarak takip eder
- **Şehir Bazlı İstatistikler**: 81 il için karşılaştırmalı veriler
- **Kategori Analizi**: 90+ ders kategorisi için detaylı istatistikler

## 🏗️ Mimari

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  GitHub Actions │────▶│  Python Scraper │────▶│    Supabase     │
│  (2 haftalık)   │     │  (Playwright)   │     │  (PostgreSQL)   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                        │
                                                        ▼
                                               ┌─────────────────┐
                                               │   Dashboard     │
                                               │   (Opsiyonel)   │
                                               └─────────────────┘
```

## 🚀 Kurulum

### 1. Repository'yi Klonla

```bash
git clone https://github.com/your-username/ozel-ders-tracker.git
cd ozel-ders-tracker
```

### 2. Supabase Kurulumu

1. [Supabase](https://supabase.com)'de yeni bir proje oluştur
2. SQL Editor'e git
3. Migration dosyalarını sırayla çalıştır:

```bash
# 1. Ana şema
supabase/migrations/001_initial_schema.sql

# 2. Tüm iller
supabase/migrations/002_all_provinces.sql

# 3. Ders kategorileri
supabase/migrations/003_all_categories.sql
```

### 3. Environment Variables

```bash
# .env dosyasını oluştur
cp .env.example .env

# Değerleri doldur
nano .env
```

Gerekli değişkenler:
- `SUPABASE_URL`: Supabase proje URL'i
- `SUPABASE_SERVICE_ROLE_KEY`: Service role key (API Settings'den)

### 4. GitHub Secrets

Repository Settings > Secrets > Actions'a ekle:
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

### 5. Local Test

```bash
# Virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Dependencies
cd scraper
pip install -r requirements.txt
playwright install chromium

# Dry run (database'e yazmaz)
python scraper.py --dry-run

# Gerçek çalıştırma
python scraper.py
```

## 📁 Dosya Yapısı

```
ozel-ders-tracker/
├── .github/
│   └── workflows/
│       └── scraper.yml          # GitHub Actions workflow
├── scraper/
│   ├── scraper.py               # Ana scraper kodu
│   └── requirements.txt         # Python bağımlılıkları
├── supabase/
│   └── migrations/
│       ├── 001_initial_schema.sql   # Ana tablolar
│       ├── 002_all_provinces.sql    # 81 il + ilçeler
│       └── 003_all_categories.sql   # Ders kategorileri
├── .env.example                 # Environment template
├── .gitignore
└── README.md
```

## 📊 Veritabanı Şeması

### Ana Tablolar

| Tablo | Açıklama |
|-------|----------|
| `platforms` | Veri kaynakları (ozelders, armut, vb.) |
| `provinces` | 81 il |
| `districts` | İlçeler |
| `lesson_categories` | Ders kategorileri (hiyerarşik) |
| `listings` | Ana ilan tablosu |
| `price_history` | Fiyat değişim geçmişi (partitioned) |
| `scrape_runs` | Scraping logları |

### Materialized Views

| View | Açıklama |
|------|----------|
| `mv_category_price_stats` | Kategori bazlı fiyat istatistikleri |
| `mv_province_price_stats` | Şehir bazlı fiyat istatistikleri |
| `mv_category_province_stats` | Kategori + Şehir kombinasyonu |
| `mv_weekly_trends` | Haftalık trend özeti |

## 🔄 Çalışma Mantığı

1. **GitHub Actions** her Pazartesi 04:00 UTC'de tetiklenir
2. **Bi-weekly check**: 2 haftada bir çalışacak şekilde kontrol yapar
3. **Playwright** ile ozelders.com'u tarar
4. **Anonim veriler** çıkarılır (fiyat, konum, ders türü)
5. **Supabase**'e upsert edilir (yeni veya güncelleme)
6. **Fiyat değişiklikleri** price_history tablosuna kaydedilir
7. **Materialized views** refresh edilir

## 📈 Örnek Sorgular

### Kategori Bazlı Ortalama Fiyatlar

```sql
SELECT * FROM mv_category_price_stats 
ORDER BY avg_price DESC 
LIMIT 10;
```

### Şehir Karşılaştırması

```sql
SELECT * FROM mv_province_price_stats 
WHERE listing_count > 10 
ORDER BY avg_price DESC;
```

### Fiyat Trendi (Matematik - İstanbul)

```sql
SELECT 
  date_trunc('week', recorded_at) as week,
  AVG(price) as avg_price
FROM price_history ph
JOIN listings l ON l.id = ph.listing_id
WHERE l.category_id = 1 AND l.province_id = 34
GROUP BY 1
ORDER BY 1;
```

## ⚠️ Yasal Uyarılar

- **KVKK Uyumu**: Sadece anonim veriler toplanır. İsim, telefon gibi kişisel veriler kesinlikle kaydedilmez.
- **Sahibinden.com**: Bu platformda scraping yasaktır ve desteklenmez.
- **Rate Limiting**: Her istek arasında 3-7 saniye bekleme süresi uygulanır.
- **robots.txt**: Platform kurallarına uyulur.

## 🛠️ Geliştirme

### Yeni Platform Ekleme

1. `scraper.py`'de yeni scraper class'ı oluştur
2. `platforms` tablosuna kayıt ekle
3. Workflow'a yeni platform seçeneği ekle

### Test

```bash
pytest tests/
```

### Kod Kalitesi

```bash
black scraper/
isort scraper/
flake8 scraper/
mypy scraper/
```

## 📝 Lisans

MIT License

## 🤝 Katkıda Bulunma

Pull request'ler memnuniyetle karşılanır. Büyük değişiklikler için önce bir issue açın.

---

**Not**: Bu proje eğitim ve araştırma amaçlıdır. Ticari kullanım için ilgili platformların kullanım koşullarını kontrol edin.
