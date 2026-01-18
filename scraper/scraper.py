"""
Türkiye Özel Ders Piyasası Scraper
==================================
Platform: ozelders.com
Sıklık: 2 haftada bir
KVKK Uyumlu: Sadece anonim veriler toplanır

Kullanım:
    python scraper.py
    python scraper.py --platform ozelders
    python scraper.py --dry-run
"""

import os
import re
import json
import asyncio
import logging
import argparse
from datetime import datetime
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, asdict
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, Page, Browser
from supabase import create_client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# =====================================================
# CONFIGURATION
# =====================================================

@dataclass
class Config:
    """Scraper configuration"""
    SUPABASE_URL: str = os.getenv('SUPABASE_URL', '')
    SUPABASE_KEY: str = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
    
    # Anti-spam settings - Agresif olmayan scraping
    MIN_DELAY: float = 8.0   # Minimum delay between requests (seconds)
    MAX_DELAY: float = 15.0  # Maximum delay between requests (seconds)
    CATEGORY_DELAY: float = 30.0  # Delay between different categories (seconds)
    PAGE_TIMEOUT: int = 45000  # Page load timeout (ms)
    MAX_RETRIES: int = 2
    MAX_PAGES_PER_CATEGORY: int = 10  # Max pages per category (100 kişi / ~20 kişi per sayfa = 5 sayfa yeterli)
    
    # Rate limiting
    REQUESTS_PER_MINUTE: int = 4  # Max 4 request per minute
    PAUSE_AFTER_REQUESTS: int = 20  # Her 20 request'te bir uzun mola
    LONG_PAUSE_DURATION: float = 120.0  # 2 dakika mola
    
    # User agent rotation
    USER_AGENTS: List[str] = None
    
    def __post_init__(self):
        self.USER_AGENTS = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        ]

config = Config()

# =====================================================
# LOGGING SETUP
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('scraper.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# =====================================================
# DATA MODELS
# =====================================================

@dataclass
class ListingData:
    """Represents a single listing (KVKK compliant - no personal data)"""
    platform_id: int
    external_id: str
    price_per_hour: Optional[float] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    category_raw: Optional[str] = None
    location_raw: Optional[str] = None
    lesson_type: Optional[str] = None  # 'online', 'in_person', 'both'
    experience_raw: Optional[str] = None
    source_url: Optional[str] = None

@dataclass
class ScrapeResult:
    """Result of a scraping run"""
    platform_id: int
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str = 'running'
    total_listings: int = 0
    new_listings: int = 0
    updated_listings: int = 0
    error_count: int = 0
    error_message: Optional[str] = None

# =====================================================
# SUPABASE CLIENT
# =====================================================

class SupabaseClient:
    """Supabase database operations"""
    
    def __init__(self):
        if not config.SUPABASE_URL or not config.SUPABASE_KEY:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
        self.client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    
    def get_platform_id(self, platform_name: str) -> int:
        """Get platform ID by name"""
        result = self.client.table('platforms').select('id').eq('name', platform_name).single().execute()
        return result.data['id']
    
    def start_scrape_run(self, platform_id: int) -> int:
        """Start a new scrape run and return its ID"""
        result = self.client.table('scrape_runs').insert({
            'platform_id': platform_id,
            'status': 'running'
        }).execute()
        return result.data[0]['id']
    
    def update_scrape_run(self, run_id: int, result: ScrapeResult):
        """Update scrape run status"""
        self.client.table('scrape_runs').update({
            'completed_at': result.completed_at.isoformat() if result.completed_at else None,
            'status': result.status,
            'total_listings': result.total_listings,
            'new_listings': result.new_listings,
            'updated_listings': result.updated_listings,
            'error_count': result.error_count,
            'error_message': result.error_message
        }).eq('id', run_id).execute()
    
    def upsert_listing(self, listing: ListingData) -> Dict[str, Any]:
        """Insert or update a listing using the database function"""
        result = self.client.rpc('upsert_listing', {
            'p_platform_id': listing.platform_id,
            'p_external_id': listing.external_id,
            'p_price_per_hour': listing.price_per_hour,
            'p_category_raw': listing.category_raw,
            'p_location_raw': listing.location_raw,
            'p_lesson_type': listing.lesson_type,
            'p_experience_raw': listing.experience_raw,
            'p_source_url': listing.source_url
        }).execute()
        return result.data
    
    def get_existing_external_ids(self, platform_id: int) -> set:
        """Get all existing external IDs for incremental scraping"""
        result = self.client.table('listings').select('external_id').eq('platform_id', platform_id).execute()
        return {row['external_id'] for row in result.data}
    
    def refresh_materialized_views(self):
        """Refresh all materialized views after scraping"""
        try:
            self.client.rpc('refresh_all_materialized_views').execute()
            logger.info("Materialized views refreshed successfully")
        except Exception as e:
            logger.warning(f"Failed to refresh materialized views: {e}")

# =====================================================
# PARSERS
# =====================================================

class PriceParser:
    """Parse Turkish price formats"""
    
    @staticmethod
    def parse(price_text: str) -> Optional[float]:
        """
        Parse price from text like:
        - "450 TL/saat"
        - "450₺"
        - "300-500 TL"
        - "Saat başı 400 TL"
        """
        if not price_text:
            return None
        
        # Clean the text
        text = price_text.strip().lower()
        text = text.replace('.', '').replace(',', '.')  # Handle Turkish number format
        
        # Find all numbers
        numbers = re.findall(r'\d+(?:\.\d+)?', text)
        
        if not numbers:
            return None
        
        # If range (300-500), return average
        if len(numbers) >= 2 and '-' in price_text:
            return (float(numbers[0]) + float(numbers[1])) / 2
        
        return float(numbers[0])
    
    @staticmethod
    def parse_range(price_text: str) -> tuple[Optional[float], Optional[float]]:
        """Parse price range, returns (min, max)"""
        if not price_text:
            return None, None
        
        text = price_text.strip().replace('.', '').replace(',', '.')
        numbers = re.findall(r'\d+(?:\.\d+)?', text)
        
        if len(numbers) >= 2:
            return float(numbers[0]), float(numbers[1])
        elif len(numbers) == 1:
            price = float(numbers[0])
            return price, price
        
        return None, None


class LocationParser:
    """Parse Turkish location formats"""
    
    # Major city mappings for normalization
    CITY_MAPPINGS = {
        'istanbul': 'İstanbul',
        'ankara': 'Ankara',
        'izmir': 'İzmir',
        'bursa': 'Bursa',
        'antalya': 'Antalya',
        'adana': 'Adana',
        'konya': 'Konya',
        'gaziantep': 'Gaziantep',
        'mersin': 'Mersin',
        'kocaeli': 'Kocaeli',
        'eskisehir': 'Eskişehir',
        'eskişehir': 'Eskişehir',
        'diyarbakir': 'Diyarbakır',
        'diyarbakır': 'Diyarbakır',
    }
    
    @classmethod
    def normalize(cls, location: str) -> str:
        """Normalize location text"""
        if not location:
            return ''
        
        location = location.strip()
        location_lower = location.lower()
        
        for key, value in cls.CITY_MAPPINGS.items():
            if key in location_lower:
                return value
        
        return location


class LessonTypeParser:
    """Parse lesson type (online/in_person/both)"""
    
    ONLINE_KEYWORDS = ['online', 'uzaktan', 'internet', 'webcam', 'zoom', 'skype']
    IN_PERSON_KEYWORDS = ['yüz yüze', 'yüzyüze', 'evde', 'eve', 'birebir', 'öğrenci evinde', 'öğretmen evinde']
    
    @classmethod
    def parse(cls, text: str) -> str:
        """Parse lesson type from text"""
        if not text:
            return 'both'
        
        text_lower = text.lower()
        
        has_online = any(kw in text_lower for kw in cls.ONLINE_KEYWORDS)
        has_in_person = any(kw in text_lower for kw in cls.IN_PERSON_KEYWORDS)
        
        if has_online and has_in_person:
            return 'both'
        elif has_online:
            return 'online'
        elif has_in_person:
            return 'in_person'
        
        return 'both'  # Default


class ExperienceParser:
    """Parse experience information"""
    
    @staticmethod
    def parse_years(text: str) -> Optional[int]:
        """Extract years of experience from text"""
        if not text:
            return None
        
        # Patterns like "5 yıl", "5+ yıl", "5 yıllık"
        patterns = [
            r'(\d+)\s*\+?\s*yıl',
            r'(\d+)\s*sene',
            r'(\d+)\s*years?'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text.lower())
            if match:
                return int(match.group(1))
        
        return None

# =====================================================
# OZELDERS.COM SCRAPER
# =====================================================

class OzeldersScaper:
    """Scraper for ozelders.com"""
    
    BASE_URL = 'https://www.ozelders.com'  # Fallback
    PLATFORM_NAME = 'ozelders'
    
    # Şehirler - Subdomain formatında kullanılacak
    CITIES = [
        'istanbul',
        'ankara', 
        'izmir',
        'bursa',
        'antalya',
        'adana',
        'konya',
        'gaziantep',
        'kocaeli',
        'mersin',
    ]
    
    # Branşlar - seviye/ders formatında
    SUBJECTS = [
        'lise/matematik',
        'lise/fizik',
        'lise/kimya',
        'lise/biyoloji',
        'lise/turkce',
        'lise/ingilizce',
        'ortaokul/matematik',
        'ortaokul/ingilizce',
        'ilkokul/matematik',
        'universite/ingilizce',
        'universite/almanca',
        'universite/fransizca',
        'universite/programlama',
        'spor/yuzme',
        'muzik/piyano',
        'muzik/gitar',
    ]
    
    # Limitler
    MAX_PER_CITY_SUBJECT = 100  # Her şehir/branş için max kayıt
    MAX_INACTIVE_DAYS = 30  # Son 30 gün aktif olmayanları atla
    
    def __init__(self, db: SupabaseClient, dry_run: bool = False):
        self.db = db
        self.dry_run = dry_run
        self.platform_id = None
        self.existing_ids: set = set()
        self.result: ScrapeResult = None
        self.city_subject_counts: Dict[str, int] = {}  # Şehir/branş sayaçları
        self.request_count: int = 0  # Anti-spam request counter
        
    def _generate_urls(self) -> List[tuple]:
        """Şehir ve branş kombinasyonlarından URL'ler oluştur - subdomain formatında"""
        urls = []
        for city in self.CITIES:
            for subject in self.SUBJECTS:
                # Format: https://istanbul.ozelders.com/ders-verenler/lise/matematik
                base_url = f'https://{city}.ozelders.com'
                path = f'/ders-verenler/{subject}'
                city_subject_key = f"{city}_{subject.split('/')[-1]}"
                urls.append((base_url, path, city_subject_key))
        logger.info(f"Generated {len(urls)} URLs ({len(self.CITIES)} cities x {len(self.SUBJECTS)} subjects)")
        return urls
        
    async def run(self):
        """Main scraping entry point"""
        logger.info(f"Starting {self.PLATFORM_NAME} scraper...")
        
        if not self.dry_run:
            self.platform_id = self.db.get_platform_id(self.PLATFORM_NAME)
            self.existing_ids = self.db.get_existing_external_ids(self.platform_id)
            run_id = self.db.start_scrape_run(self.platform_id)
        else:
            self.platform_id = 1
            run_id = None
        
        self.result = ScrapeResult(
            platform_id=self.platform_id,
            started_at=datetime.now()
        )
        
        # URL'leri oluştur
        category_urls = self._generate_urls()
        
        try:
            async with async_playwright() as p:
                browser = await self._launch_browser(p)
                
                try:
                    for i, (base_url, path, city_subject_key) in enumerate(category_urls):
                        logger.info(f"\n📚 Category {i+1}/{len(category_urls)}")
                        await self._scrape_category(browser, base_url, path, city_subject_key)
                        
                        # Kategoriler arası uzun mola
                        if i < len(category_urls) - 1:  # Son kategori değilse
                            await self._category_delay()
                    
                    self.result.status = 'completed'
                    
                except Exception as e:
                    logger.error(f"Scraping error: {e}")
                    self.result.status = 'partial'
                    self.result.error_message = str(e)
                    self.result.error_count += 1
                
                finally:
                    await browser.close()
        
        except Exception as e:
            logger.error(f"Browser launch error: {e}")
            self.result.status = 'failed'
            self.result.error_message = str(e)
        
        self.result.completed_at = datetime.now()
        
        if not self.dry_run and run_id:
            self.db.update_scrape_run(run_id, self.result)
            self.db.refresh_materialized_views()
        
        self._log_summary()
        return self.result
    
    async def _launch_browser(self, playwright) -> Browser:
        """Launch browser with stealth settings"""
        import random
        
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
            ]
        )
        return browser
    
    async def _create_page(self, browser: Browser) -> Page:
        """Create a new page with random user agent"""
        import random
        
        context = await browser.new_context(
            user_agent=random.choice(config.USER_AGENTS),
            viewport={'width': 1920, 'height': 1080},
            locale='tr-TR',
        )
        
        page = await context.new_page()
        page.set_default_timeout(config.PAGE_TIMEOUT)
        
        # Add stealth scripts
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)
        
        return page
    
    async def _scrape_category(self, browser: Browser, base_url: str, path: str, city_subject_key: str):
        """Scrape all listings in a category with limits"""
        full_url = f"{base_url}{path}"
        
        # Bu şehir/branş için mevcut sayaç
        current_count = self.city_subject_counts.get(city_subject_key, 0)
        if current_count >= self.MAX_PER_CITY_SUBJECT:
            logger.info(f"  Skipping {city_subject_key}: already at limit ({current_count})")
            return
        
        logger.info(f"Scraping: {full_url} (current count: {current_count})")
        
        page = await self._create_page(browser)

        # Kategori boyunca görülen ID'leri takip et (sayfalar arası duplicate önleme)
        category_seen_ids = set()

        try:
            page_num = 1
            category_count = 0

            while page_num <= config.MAX_PAGES_PER_CATEGORY:
                # Limit kontrolü
                if self.city_subject_counts.get(city_subject_key, 0) >= self.MAX_PER_CITY_SUBJECT:
                    logger.info(f"  Reached limit for {city_subject_key}, stopping.")
                    break
                
                paginated_url = f"{full_url}?sayfa={page_num}" if page_num > 1 else full_url
                
                logger.info(f"  Page {page_num}: {paginated_url}")
                
                try:
                    await page.goto(paginated_url, wait_until='networkidle')
                    await asyncio.sleep(1)  # Wait for dynamic content
                    
                    listings = await self._extract_listings(page, path, city_subject_key, category_seen_ids)

                    if not listings:
                        logger.info(f"  No more listings found, stopping.")
                        break
                    
                    # Listings'i işle (limit kontrolü ile)
                    for listing in listings:
                        if self.city_subject_counts.get(city_subject_key, 0) >= self.MAX_PER_CITY_SUBJECT:
                            break
                        
                        saved = await self._process_listing(listing, city_subject_key)
                        if saved:
                            category_count += 1
                    
                    # Check if there's a next page
                    has_next = await self._has_next_page(page)
                    if not has_next:
                        break
                    
                    page_num += 1
                    await self._random_delay()
                    
                except Exception as e:
                    logger.error(f"  Error on page {page_num}: {e}")
                    self.result.error_count += 1
                    break
            
            logger.info(f"  Category total: {category_count} saved for {city_subject_key}")
        
        finally:
            await page.close()
    
    async def _extract_listings(self, page: Page, path: str, city_subject_key: str, category_seen_ids: set = None) -> List[ListingData]:
        """Extract listings from the current page"""
        import re

        if category_seen_ids is None:
            category_seen_ids = set()

        listings = []

        # Sayfa içeriğini al
        all_text = await page.inner_text('body')

        # "Öne Çıkan Ders Verenler" bölümünü ayır - sadece ana listeyi al
        main_section = all_text
        if 'Öne Çıkan Ders Verenler' in all_text:
            main_section = all_text.split('Öne Çıkan Ders Verenler')[0]
            logger.info(f"    Filtered out 'Öne Çıkan' section")

        # Ayrıca "Başarı Hikayeleri" ve footer'ı da çıkar
        if 'Başarı Hikayeleri' in main_section:
            main_section = main_section.split('Başarı Hikayeleri')[0]

        # Debug: Sayfa içeriğinin bir kısmını logla
        logger.debug(f"    Main section preview: {main_section[:500]}...")

        # Öğretmen bloklarını bul - "bu yana üye" pattern'i ile split
        # Her öğretmen kartı "XXXX'den bu yana üye" veya "XXXX'dan bu yana üye" içerir
        # Pattern: 2015'den bu yana üye, 2020'den bu yana üye, vb.
        blocks = re.split(r"(\d{4}'[dD]?[eE]?n bu yana üye)", main_section)

        # Split sonucu: [önce, "2015'den bu yana üye", arada, "2020'den bu yana üye", sonra, ...]
        # Blokları birleştir: her öğretmen = önceki text + "xxxx'den bu yana üye"
        teacher_blocks = []
        for i in range(0, len(blocks) - 1, 2):
            if i + 1 < len(blocks):
                block = blocks[i] + blocks[i + 1]
                teacher_blocks.append(block)

        logger.info(f"    Found {len(teacher_blocks)} potential teacher blocks (split by 'bu yana üye')")
        
        seen_names = set()  # İsim bazlı duplicate kontrolü
        skipped_inactive = 0
        
        skipped_premium = 0
        skipped_duplicate = 0

        for i, block in enumerate(teacher_blocks):
            if 'TL' not in block:
                continue

            # *** PREMIUM/TANITIM KONTROLÜ ***
            # Ücretli ilanları atla - bunlar her sayfada tekrar gösteriliyor
            premium_markers = ['Tanıtım', 'TANITIM', 'Sponsorlu', 'SPONSORLU', 'Premium', 'PREMIUM', 'Reklam']
            is_premium = any(marker in block for marker in premium_markers)
            if is_premium:
                skipped_premium += 1
                continue

            # *** AKTİVİTE KONTROLÜ ***
            # "Bugün", "1 gün önce", "2 gün önce" ... "4 hafta önce" kabul edilir
            # "1 ay önce", "2 ay önce" gibi olanlar atlanır
            if not self._is_recently_active(block):
                skipped_inactive += 1
                continue
            
            # İsmi çıkar - blok içindeki ilk geçerli isim satırı
            lines = block.strip().split('\n')
            if not lines:
                continue

            name_line = None

            # İsim değil kelimeleri (atlanacak pattern'ler)
            skip_patterns = [
                r'^Ders Verenler', r'^İstanbul', r'^Ankara', r'^İzmir', r'^Bursa',
                r'^Antalya', r'^Adana', r'^Konya', r'^Gaziantep', r'^Kocaeli', r'^Mersin',
                r'^Online', r'^Offline', r'^Öne Çıkan', r'^Başarı', r'^Bize Ulaşın',
                r'^Copyright', r'^\d+$', r'^TL', r'^Tüm ', r'^Onaylı', r'^Tanıtım',
                r'^Nasıl Çalışır', r'^Eğitmen Ara', r'^Blog', r'^Yardım', r'^Ders',
                r'^Matematik', r'^Fizik', r'^Kimya', r'^Biyoloji', r'^Türkçe', r'^İngilizce',
                r'^Lise', r'^Ortaokul', r'^İlkokul', r'^Üniversite', r'^Spor', r'^Müzik',
                r'^OZELDERS', r'^Bugün', r'gün önce$', r'hafta önce$', r'ay önce$',
                r'^Kadıköy', r'^Beşiktaş', r'^Bakırköy', r'^Üsküdar', r'^Kartal',
                r'^Cambridge', r'^CAMBRIDGE', r'^Öğretmen', r'^Öğretmeni',
            ]

            for line in lines:
                line = line.strip()
                if not line or len(line) < 3:
                    continue

                # Skip pattern kontrolü
                should_skip = False
                for pattern in skip_patterns:
                    if re.search(pattern, line, re.IGNORECASE):
                        should_skip = True
                        break

                if should_skip:
                    continue

                # İsim kriterleri:
                # 1. En az 2 kelime (veya "Ad S." formatı)
                # 2. Max 40 karakter
                # 3. Sayı ile başlamamalı
                # 4. TL içermemeli
                words = line.split()
                if len(words) < 2:
                    continue
                if len(line) > 40:
                    continue
                if re.match(r'^\d', line):
                    continue
                if 'TL' in line or '₺' in line:
                    continue
                # İçinde fiyat olmamalı
                if re.search(r'\d{3,}', line):
                    continue

                # Bu muhtemelen isim
                name_line = line
                break

            if not name_line:
                continue
            
            # Duplicate isim kontrolü (aynı sayfada)
            if name_line in seen_names:
                continue
            seen_names.add(name_line)
            
            try:
                listing = self._parse_text_block(block, path, city_subject_key, i, name_line)
                if listing:
                    # Kategori boyunca duplicate kontrolü (önceki sayfalarda görüldü mü?)
                    if listing.external_id in category_seen_ids:
                        skipped_duplicate += 1
                        continue
                    category_seen_ids.add(listing.external_id)
                    listings.append(listing)
                    logger.debug(f"      ✓ Parsed: {name_line[:30]} -> {listing.external_id} ({listing.price_per_hour} TL)")
                else:
                    logger.debug(f"      ✗ No price: {name_line[:30]}")
            except Exception as e:
                logger.warning(f"Failed to parse block: {e}")
                continue
        
        if skipped_premium > 0:
            logger.info(f"    Skipped {skipped_premium} premium/sponsored listings")
        if skipped_duplicate > 0:
            logger.info(f"    Skipped {skipped_duplicate} duplicates (seen in previous pages)")
        if skipped_inactive > 0:
            logger.info(f"    Skipped {skipped_inactive} inactive users (>30 days)")

        # Kaydedilen isimleri logla
        if listings:
            names_preview = [l.external_id for l in listings[:5]]
            logger.info(f"    Extracted {len(listings)} active unique listings: {names_preview}...")
        else:
            logger.info(f"    Extracted 0 listings")
        return listings
    
    def _is_recently_active(self, block: str) -> bool:
        """Son 30 gün içinde aktif mi kontrol et"""
        import re
        
        # Aktif kabul edilen pattern'ler
        active_patterns = [
            r'Bugün',
            r'\d+\s*gün önce',      # 1 gün önce, 2 gün önce, ... 
            r'\d+\s*hafta önce',    # 1 hafta önce, 2 hafta önce, 3 hafta önce, 4 hafta önce
        ]
        
        for pattern in active_patterns:
            if re.search(pattern, block):
                # Hafta kontrolü - 4 haftadan fazla = ~1 ay
                hafta_match = re.search(r'(\d+)\s*hafta önce', block)
                if hafta_match:
                    weeks = int(hafta_match.group(1))
                    if weeks > 4:
                        return False
                return True
        
        # "ay önce" varsa aktif değil
        if re.search(r'\d+\s*ay önce', block):
            return False
        
        # "yıl önce" varsa aktif değil
        if re.search(r'\d+\s*yıl önce', block):
            return False
        
        # Hiçbir aktivite bilgisi yoksa kabul et (muhtemelen yeni üye)
        return True
    
    def _parse_text_block(self, block: str, path: str, city_subject_key: str, index: int, name: str = None) -> Optional[ListingData]:
        """Parse a text block containing teacher info"""
        import re
        import hashlib
        
        # İsim yoksa bloktan çıkar
        if not name:
            lines = block.strip().split('\n')
            name = lines[0].strip() if lines else f"unknown_{index}"
        
        # Branş bilgisini al: city_subject_key = "istanbul_matematik" -> "matematik"
        subject = city_subject_key.split('_')[-1] if '_' in city_subject_key else 'unknown'
        
        # External ID - isim + branş bazlı hash (tutarlı ve unique)
        # İsmi normalize et: küçük harf, boşlukları _ ile değiştir
        name_normalized = name.lower().replace(' ', '_').replace('.', '')
        # Türkçe karakterleri dönüştür
        tr_chars = {'ı': 'i', 'ğ': 'g', 'ü': 'u', 'ş': 's', 'ö': 'o', 'ç': 'c',
                    'İ': 'i', 'Ğ': 'g', 'Ü': 'u', 'Ş': 's', 'Ö': 'o', 'Ç': 'c'}
        for tr, en in tr_chars.items():
            name_normalized = name_normalized.replace(tr, en)
        
        # İsim + branş kombinasyonu ile hash oluştur
        # Böylece aynı kişi farklı branşlarda farklı ID alır
        unique_key = f"{name_normalized}_{subject}"
        name_hash = hashlib.md5(unique_key.encode()).hexdigest()[:8]
        external_id = f"oz_{name_hash}"
        
        # Fiyat - "850 TL/Saat" veya "2000 - 4000 TL/Saat" formatında
        price = None
        
        # Önce aralık kontrolü
        range_match = re.search(r'(\d+)\s*-\s*(\d+)\s*TL', block)
        if range_match:
            min_price = float(range_match.group(1))
            max_price = float(range_match.group(2))
            price = (min_price + max_price) / 2
        else:
            # Tek fiyat
            price_match = re.search(r'(\d+)\s*TL/?[Ss]aat', block)
            if price_match:
                price = float(price_match.group(1))
        
        # Fiyat yoksa bu kişiyi atla (ücretsiz üyeler fiyat göstermiyor)
        if not price:
            return None
        
        # Konum - city_subject_key'den şehir bilgisini al
        # city_subject_key format: "istanbul_matematik"
        city_raw = city_subject_key.split('_')[0] if '_' in city_subject_key else None
        
        city_map = {
            'istanbul': 'İstanbul',
            'ankara': 'Ankara',
            'izmir': 'İzmir',
            'bursa': 'Bursa',
            'antalya': 'Antalya',
            'adana': 'Adana',
            'konya': 'Konya',
            'gaziantep': 'Gaziantep',
            'kocaeli': 'Kocaeli',
            'mersin': 'Mersin',
        }
        
        location = city_map.get(city_raw)
        
        # İlçe bilgisini bloktan çıkarmaya çalış
        location_match = re.search(r'([A-Za-zığüşöçİĞÜŞÖÇ]+),\s*(İstanbul|Ankara|İzmir|Bursa|Antalya|Konya|Gaziantep|Adana|Mersin|Kocaeli)', block)
        if location_match:
            location = f"{location_match.group(1)}, {location_match.group(2)}"
        
        # Online/Offline
        lesson_type = 'both'
        if 'Online Ders Veren' in block:
            lesson_type = 'online'
        
        # Deneyim - üyelik yılından hesapla
        experience_raw = None
        exp_match = re.search(r"(\d{4})'?[dD]?[eE]?n bu yana", block)
        if exp_match:
            start_year = int(exp_match.group(1))
            years = 2026 - start_year
            experience_raw = f"{years} yıl"
        
        # Kategori - path'den al: /ders-verenler/lise/matematik -> matematik
        category_raw = path.split('/')[-1] if path else None
        
        return ListingData(
            platform_id=self.platform_id,
            external_id=external_id,
            price_per_hour=price,
            category_raw=category_raw,
            location_raw=location,
            lesson_type=lesson_type,
            experience_raw=experience_raw,
            source_url=None
        )
    
    async def _parse_listing_card(self, card, category_url: str) -> Optional[ListingData]:
        """Parse a single listing card element"""
        try:
            card_text = await card.inner_text()
            
            # External ID - profil linkinden al
            link_elem = await card.query_selector('a[href*="/uye/"], a[href*="/profil/"], a[href*="/ogretmen/"]')
            if link_elem:
                href = await link_elem.get_attribute('href')
                external_id = self._extract_id_from_url(href)
            else:
                # Hash ile unique ID oluştur
                external_id = f"ozelders_{abs(hash(card_text)) % 10000000}"
            
            # Fiyat - "850 TL/Saat" veya "2000 - 4000 TL/Saat" formatında
            price = None
            price_patterns = [
                r'(\d+(?:\.\d+)?)\s*(?:-\s*\d+(?:\.\d+)?)?\s*TL/?[Ss]aat',
                r'(\d+)\s*TL',
            ]
            for pattern in price_patterns:
                match = re.search(pattern, card_text)
                if match:
                    price = float(match.group(1).replace('.', ''))
                    break
            
            # Eğer fiyat aralığı varsa ortalama al
            range_match = re.search(r'(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*TL', card_text)
            if range_match:
                min_price = float(range_match.group(1).replace('.', ''))
                max_price = float(range_match.group(2).replace('.', ''))
                price = (min_price + max_price) / 2
            
            # Konum - "Beşiktaş, İstanbul" formatında
            location = None
            location_match = re.search(r'([A-Za-zığüşöçİĞÜŞÖÇ]+),\s*(İstanbul|Ankara|İzmir|Bursa|Antalya|[A-Za-zığüşöçİĞÜŞÖÇ]+)', card_text)
            if location_match:
                location = f"{location_match.group(1)}, {location_match.group(2)}"
            else:
                # Sadece şehir
                cities = ['İstanbul', 'Ankara', 'İzmir', 'Bursa', 'Antalya', 'Adana', 'Konya', 'Gaziantep']
                for city in cities:
                    if city in card_text:
                        location = city
                        break
            
            # Online/Offline
            lesson_type = 'both'
            if 'Online Ders Veren' in card_text or 'Online' in card_text:
                lesson_type = 'online'
            if 'Offline' in card_text:
                lesson_type = 'in_person' if lesson_type == 'both' else 'both'
            
            # Deneyim - "2015'den bu yana üye" formatından yıl hesapla
            experience_raw = None
            exp_match = re.search(r"(\d{4})'den bu yana", card_text)
            if exp_match:
                start_year = int(exp_match.group(1))
                years = 2026 - start_year
                experience_raw = f"{years} yıl"
            
            # Kategori
            category_raw = category_url.split('/')[-1] if category_url else None
            
            # Source URL
            source_url = None
            if link_elem:
                href = await link_elem.get_attribute('href')
                source_url = urljoin(self.BASE_URL, href)
            
            # Fiyat yoksa bu kaydı atla
            if not price:
                return None
            
            return ListingData(
                platform_id=self.platform_id,
                external_id=external_id,
                price_per_hour=price,
                category_raw=category_raw,
                location_raw=location,
                lesson_type=lesson_type,
                experience_raw=experience_raw,
                source_url=source_url
            )
            
        except Exception as e:
            logger.warning(f"Error parsing listing: {e}")
            return None
    
    def _extract_id_from_url(self, url: str) -> str:
        """Extract unique ID from URL"""
        # Example: /ogretmen/12345-ahmet -> 12345
        match = re.search(r'/(?:ogretmen|teacher)/(\d+)', url)
        if match:
            return match.group(1)
        
        # Fallback: use last path segment
        path = urlparse(url).path
        return path.split('/')[-1] or f"hash_{hash(url)}"
    
    async def _has_next_page(self, page: Page) -> bool:
        """Check if there's a next page"""
        # ozelders.com pagination selectors
        selectors = [
            'a.page-link[href*="sayfa"]',  # Bootstrap pagination
            'a[href*="sayfa="]',
            '.pagination a:not(.disabled)',
            'a.next',
            'a[rel="next"]',
            '[class*="sonraki"]',
            'li.page-item:not(.disabled) a',
            'a:has-text("»")',
            'a:has-text("Sonraki")',
        ]
        
        for selector in selectors:
            try:
                next_btn = await page.query_selector(selector)
                if next_btn:
                    href = await next_btn.get_attribute('href')
                    logger.info(f"    Found next page link: {href}")
                    return True
            except:
                continue
        
        # Alternatif: Sayfa numaralarını kontrol et
        page_content = await page.content()
        current_url = page.url
        
        # URL'den mevcut sayfa numarasını al
        import re
        current_page_match = re.search(r'sayfa=(\d+)', current_url)
        current_page = int(current_page_match.group(1)) if current_page_match else 1
        
        # Sayfa içeriğinde daha yüksek sayfa numarası var mı?
        page_numbers = re.findall(r'sayfa=(\d+)', page_content)
        if page_numbers:
            max_page = max(int(p) for p in page_numbers)
            if max_page > current_page:
                logger.info(f"    Found higher page number: {max_page} (current: {current_page})")
                return True
        
        logger.info(f"    No next page found")
        return False
    
    async def _process_listing(self, listing: ListingData, city_subject_key: str = None) -> bool:
        """Process and save a listing. Returns True if saved."""
        self.result.total_listings += 1
        
        if self.dry_run:
            logger.info(f"  [DRY RUN] Would save: {listing.external_id} - {listing.price_per_hour} TL")
            if city_subject_key:
                self.city_subject_counts[city_subject_key] = self.city_subject_counts.get(city_subject_key, 0) + 1
            return True
        
        is_new = listing.external_id not in self.existing_ids
        
        try:
            self.db.upsert_listing(listing)
            self.existing_ids.add(listing.external_id)
            
            # Şehir/branş sayacını artır
            if city_subject_key:
                self.city_subject_counts[city_subject_key] = self.city_subject_counts.get(city_subject_key, 0) + 1
            
            if is_new:
                self.result.new_listings += 1
            else:
                self.result.updated_listings += 1
            
            return True
                
        except Exception as e:
            logger.error(f"Failed to save listing {listing.external_id}: {e}")
            self.result.error_count += 1
            return False
    
    async def _random_delay(self):
        """Random delay between requests with anti-spam protection"""
        import random
        
        self.request_count += 1
        
        # Her X request'te uzun mola ver
        if self.request_count % config.PAUSE_AFTER_REQUESTS == 0:
            logger.info(f"    🛑 Anti-spam pause: {config.LONG_PAUSE_DURATION}s after {self.request_count} requests")
            await asyncio.sleep(config.LONG_PAUSE_DURATION)
        else:
            # Normal random delay
            delay = random.uniform(config.MIN_DELAY, config.MAX_DELAY)
            await asyncio.sleep(delay)
    
    async def _category_delay(self):
        """Longer delay between categories"""
        import random
        delay = config.CATEGORY_DELAY + random.uniform(0, 10)
        logger.info(f"  ⏸️ Category delay: {delay:.1f}s")
        await asyncio.sleep(delay)
    
    def _log_summary(self):
        """Log scraping summary"""
        duration = (self.result.completed_at - self.result.started_at).total_seconds()
        
        logger.info("=" * 50)
        logger.info("SCRAPING SUMMARY")
        logger.info("=" * 50)
        logger.info(f"Platform: {self.PLATFORM_NAME}")
        logger.info(f"Status: {self.result.status}")
        logger.info(f"Duration: {duration:.1f} seconds ({duration/60:.1f} minutes)")
        logger.info(f"Total requests: {self.request_count}")
        logger.info(f"Total listings: {self.result.total_listings}")
        logger.info(f"New listings: {self.result.new_listings}")
        logger.info(f"Updated listings: {self.result.updated_listings}")
        logger.info(f"Errors: {self.result.error_count}")
        
        # Şehir/branş istatistikleri
        logger.info("-" * 30)
        logger.info("City/Subject counts:")
        for key, count in sorted(self.city_subject_counts.items()):
            logger.info(f"  {key}: {count}")
        
        if self.result.error_message:
            logger.info(f"Error message: {self.result.error_message}")
        logger.info("=" * 50)

# =====================================================
# MAIN ENTRY POINT
# =====================================================

async def main():
    parser = argparse.ArgumentParser(description='Türkiye Özel Ders Piyasası Scraper')
    parser.add_argument('--platform', default='ozelders', choices=['ozelders'],
                        help='Platform to scrape')
    parser.add_argument('--dry-run', action='store_true',
                        help='Run without saving to database')
    parser.add_argument('--debug', action='store_true',
                        help='Enable debug logging')
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    if args.dry_run:
        logger.info("Running in DRY RUN mode - no data will be saved")
        db = None
    else:
        db = SupabaseClient()
    
    if args.platform == 'ozelders':
        scraper = OzeldersScaper(db, dry_run=args.dry_run)
    else:
        raise ValueError(f"Unknown platform: {args.platform}")
    
    result = await scraper.run()
    
    # Exit with error code if scraping failed
    if result.status == 'failed':
        exit(1)
    elif result.error_count > result.total_listings * 0.1:  # More than 10% errors
        exit(1)

if __name__ == '__main__':
    asyncio.run(main())
