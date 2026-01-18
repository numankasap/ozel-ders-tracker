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
    
    # Scraping settings
    MIN_DELAY: float = 3.0  # Minimum delay between requests (seconds)
    MAX_DELAY: float = 7.0  # Maximum delay between requests (seconds)
    PAGE_TIMEOUT: int = 30000  # Page load timeout (ms)
    MAX_RETRIES: int = 3
    MAX_PAGES_PER_CATEGORY: int = 50  # Max pages to scrape per category
    
    # User agent rotation
    USER_AGENTS: List[str] = None
    
    def __post_init__(self):
        self.USER_AGENTS = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
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
    
    BASE_URL = 'https://www.ozelders.com'
    PLATFORM_NAME = 'ozelders'
    
    # Category URLs to scrape - Doğru URL yapısı: /ders-verenler/{seviye}/{ders}
    CATEGORIES = [
        '/ders-verenler/lise/matematik',
        '/ders-verenler/lise/fizik',
        '/ders-verenler/lise/kimya',
        '/ders-verenler/lise/biyoloji',
        '/ders-verenler/lise/turkce',
        '/ders-verenler/universite/ingilizce',
        '/ders-verenler/universite/almanca',
        '/ders-verenler/universite/fransizca',
        '/ders-verenler/universite/piyano',
        '/ders-verenler/universite/gitar',
        '/ders-verenler/universite/programlama',
        '/ders-verenler/universite/yuzme',
        '/ders-verenler/ortaokul/matematik',
        '/ders-verenler/ilkokul/matematik',
    ]
    
    def __init__(self, db: SupabaseClient, dry_run: bool = False):
        self.db = db
        self.dry_run = dry_run
        self.platform_id = None
        self.existing_ids: set = set()
        self.result: ScrapeResult = None
        
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
        
        try:
            async with async_playwright() as p:
                browser = await self._launch_browser(p)
                
                try:
                    for category_url in self.CATEGORIES:
                        await self._scrape_category(browser, category_url)
                        await self._random_delay()
                    
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
    
    async def _scrape_category(self, browser: Browser, category_url: str):
        """Scrape all listings in a category"""
        full_url = urljoin(self.BASE_URL, category_url)
        logger.info(f"Scraping category: {full_url}")
        
        page = await self._create_page(browser)
        
        try:
            page_num = 1
            while page_num <= config.MAX_PAGES_PER_CATEGORY:
                paginated_url = f"{full_url}?sayfa={page_num}" if page_num > 1 else full_url
                
                logger.info(f"  Page {page_num}: {paginated_url}")
                
                try:
                    await page.goto(paginated_url, wait_until='networkidle')
                    await asyncio.sleep(1)  # Wait for dynamic content
                    
                    listings = await self._extract_listings(page, category_url)
                    
                    if not listings:
                        logger.info(f"  No more listings found, stopping.")
                        break
                    
                    for listing in listings:
                        await self._process_listing(listing)
                    
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
        
        finally:
            await page.close()
    
    async def _extract_listings(self, page: Page, category_url: str) -> List[ListingData]:
        """Extract listings from the current page"""
        listings = []
        
        # ozelders.com specific selectors - adjust based on actual site structure
        # These are example selectors and may need adjustment
        listing_cards = await page.query_selector_all('.ogretmen-listesi .ogretmen-kutu, .teacher-card, .listing-item')
        
        if not listing_cards:
            # Try alternative selectors
            listing_cards = await page.query_selector_all('[class*="ogretmen"], [class*="teacher"], [class*="listing"]')
        
        for card in listing_cards:
            try:
                listing = await self._parse_listing_card(card, category_url)
                if listing:
                    listings.append(listing)
            except Exception as e:
                logger.warning(f"Failed to parse listing card: {e}")
                continue
        
        return listings
    
    async def _parse_listing_card(self, card, category_url: str) -> Optional[ListingData]:
        """Parse a single listing card element"""
        try:
            # Extract external ID from link or data attribute
            link_elem = await card.query_selector('a[href*="/ogretmen/"], a[href*="/teacher/"]')
            if link_elem:
                href = await link_elem.get_attribute('href')
                external_id = self._extract_id_from_url(href)
            else:
                # Generate ID from position if no link
                external_id = f"ozelders_{hash(await card.inner_text())}"
            
            # Extract price
            price_elem = await card.query_selector('[class*="fiyat"], [class*="price"], [class*="ucret"]')
            price_text = await price_elem.inner_text() if price_elem else None
            price = PriceParser.parse(price_text)
            
            # Extract location
            location_elem = await card.query_selector('[class*="konum"], [class*="location"], [class*="sehir"], [class*="il"]')
            location_text = await location_elem.inner_text() if location_elem else None
            location = LocationParser.normalize(location_text)
            
            # Extract lesson type
            type_elem = await card.query_selector('[class*="ders-tipi"], [class*="lesson-type"], [class*="online"]')
            type_text = await type_elem.inner_text() if type_elem else None
            
            # Also check for online/offline badges
            online_badge = await card.query_selector('[class*="online"], .badge-online')
            if online_badge:
                type_text = (type_text or '') + ' online'
            
            lesson_type = LessonTypeParser.parse(type_text or await card.inner_text())
            
            # Extract experience
            exp_elem = await card.query_selector('[class*="deneyim"], [class*="experience"], [class*="yil"]')
            exp_text = await exp_elem.inner_text() if exp_elem else None
            
            # Extract category from URL
            category_raw = category_url.split('/')[-1] if category_url else None
            
            # Build source URL
            source_url = None
            if link_elem:
                href = await link_elem.get_attribute('href')
                source_url = urljoin(self.BASE_URL, href)
            
            return ListingData(
                platform_id=self.platform_id,
                external_id=external_id,
                price_per_hour=price,
                category_raw=category_raw,
                location_raw=location,
                lesson_type=lesson_type,
                experience_raw=exp_text,
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
        next_btn = await page.query_selector('a.next, a[rel="next"], .pagination .next:not(.disabled), [class*="sonraki"]')
        return next_btn is not None
    
    async def _process_listing(self, listing: ListingData):
        """Process and save a listing"""
        self.result.total_listings += 1
        
        if self.dry_run:
            logger.info(f"  [DRY RUN] Would save: {listing.external_id} - {listing.price_per_hour} TL")
            return
        
        is_new = listing.external_id not in self.existing_ids
        
        try:
            self.db.upsert_listing(listing)
            self.existing_ids.add(listing.external_id)
            
            if is_new:
                self.result.new_listings += 1
            else:
                self.result.updated_listings += 1
                
        except Exception as e:
            logger.error(f"Failed to save listing {listing.external_id}: {e}")
            self.result.error_count += 1
    
    async def _random_delay(self):
        """Random delay between requests"""
        import random
        delay = random.uniform(config.MIN_DELAY, config.MAX_DELAY)
        await asyncio.sleep(delay)
    
    def _log_summary(self):
        """Log scraping summary"""
        duration = (self.result.completed_at - self.result.started_at).total_seconds()
        
        logger.info("=" * 50)
        logger.info("SCRAPING SUMMARY")
        logger.info("=" * 50)
        logger.info(f"Platform: {self.PLATFORM_NAME}")
        logger.info(f"Status: {self.result.status}")
        logger.info(f"Duration: {duration:.1f} seconds")
        logger.info(f"Total listings: {self.result.total_listings}")
        logger.info(f"New listings: {self.result.new_listings}")
        logger.info(f"Updated listings: {self.result.updated_listings}")
        logger.info(f"Errors: {self.result.error_count}")
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
