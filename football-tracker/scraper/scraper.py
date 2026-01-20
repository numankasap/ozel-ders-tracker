#!/usr/bin/env python3
"""
Türk Futbolu Veri Toplayıcı
- API-Football: Oyuncu istatistikleri, maç verileri
- Transfermarkt: Piyasa değerleri, transfer bilgileri
"""

import os
import re
import time
import json
import logging
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from bs4 import BeautifulSoup

# Logging ayarları
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# API Ayarları
API_FOOTBALL_KEY = os.environ.get('API_FOOTBALL_KEY', '')
API_FOOTBALL_HOST = 'api-football-v1.p.rapidapi.com'
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

# Türkiye Ligleri (API-Football ID'leri)
TURKISH_LEAGUES = {
    203: {'name': 'Süper Lig', 'tier': 1},
    204: {'name': 'TFF 1. Lig', 'tier': 2},
    205: {'name': 'TFF 2. Lig', 'tier': 3},
    206: {'name': 'TFF 3. Lig', 'tier': 4},
    207: {'name': 'Türkiye Kupası', 'tier': 0},
}

# Rate limiting
REQUEST_DELAY = 6  # API-Football free tier: 10 req/min = 6 saniye
TRANSFERMARKT_DELAY = 3  # Transfermarkt için 3 saniye


class SupabaseClient:
    """Supabase REST API client"""

    def __init__(self, url: str, key: str):
        self.url = url.rstrip('/')
        self.headers = {
            'apikey': key,
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'Prefer': 'return=representation'
        }

    def select(self, table: str, params: Dict = None) -> List[Dict]:
        """SELECT query"""
        url = f"{self.url}/rest/v1/{table}"
        if params:
            query_params = '&'.join([f"{k}={v}" for k, v in params.items()])
            url = f"{url}?{query_params}"

        response = requests.get(url, headers=self.headers)
        if response.status_code == 200:
            return response.json()
        logger.error(f"Select error: {response.status_code} - {response.text}")
        return []

    def upsert(self, table: str, data: Dict | List[Dict]) -> bool:
        """UPSERT (insert or update on conflict)"""
        url = f"{self.url}/rest/v1/{table}"
        headers = {**self.headers, 'Prefer': 'resolution=merge-duplicates'}

        if isinstance(data, dict):
            data = [data]

        response = requests.post(url, headers=headers, json=data)
        if response.status_code in [200, 201]:
            return True
        logger.error(f"Upsert error: {response.status_code} - {response.text}")
        return False

    def insert(self, table: str, data: Dict | List[Dict]) -> bool:
        """INSERT"""
        url = f"{self.url}/rest/v1/{table}"

        if isinstance(data, dict):
            data = [data]

        response = requests.post(url, headers=self.headers, json=data)
        if response.status_code in [200, 201]:
            return True
        logger.error(f"Insert error: {response.status_code} - {response.text}")
        return False


class APIFootballClient:
    """API-Football client"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = 'https://api-football-v1.p.rapidapi.com/v3'
        self.headers = {
            'x-rapidapi-key': api_key,
            'x-rapidapi-host': API_FOOTBALL_HOST
        }
        self.last_request_time = 0

    def _rate_limit(self):
        """Rate limiting uygula"""
        elapsed = time.time() - self.last_request_time
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        self.last_request_time = time.time()

    def _request(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """API isteği yap"""
        self._rate_limit()

        url = f"{self.base_url}/{endpoint}"
        try:
            response = requests.get(url, headers=self.headers, params=params)
            if response.status_code == 200:
                data = response.json()
                if data.get('errors'):
                    logger.error(f"API error: {data['errors']}")
                    return None
                return data
            logger.error(f"Request failed: {response.status_code}")
            return None
        except Exception as e:
            logger.error(f"Request exception: {e}")
            return None

    def get_teams(self, league_id: int, season: int) -> List[Dict]:
        """Lig takımlarını getir"""
        data = self._request('teams', {'league': league_id, 'season': season})
        if data and 'response' in data:
            return data['response']
        return []

    def get_team_squad(self, team_id: int) -> List[Dict]:
        """Takım kadrosunu getir"""
        data = self._request('players/squads', {'team': team_id})
        if data and 'response' in data and data['response']:
            return data['response'][0].get('players', [])
        return []

    def get_player_stats(self, player_id: int, season: int) -> Optional[Dict]:
        """Oyuncu sezon istatistiklerini getir"""
        data = self._request('players', {'id': player_id, 'season': season})
        if data and 'response' in data and data['response']:
            return data['response'][0]
        return None

    def get_league_players(self, league_id: int, season: int, page: int = 1) -> Dict:
        """Lig oyuncularını sayfalı getir"""
        data = self._request('players', {
            'league': league_id,
            'season': season,
            'page': page
        })
        if data:
            return {
                'players': data.get('response', []),
                'paging': data.get('paging', {})
            }
        return {'players': [], 'paging': {}}

    def get_player_fixtures(self, player_id: int, season: int) -> List[Dict]:
        """Oyuncunun maç performanslarını getir"""
        # Bu endpoint Pro plan gerektirebilir
        data = self._request('fixtures/players', {
            'player': player_id,
            'season': season
        })
        if data and 'response' in data:
            return data['response']
        return []


class TransfermarktScraper:
    """Transfermarkt web scraper"""

    def __init__(self):
        self.base_url = 'https://www.transfermarkt.com.tr'
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7'
        }
        self.last_request_time = 0

    def _rate_limit(self):
        """Rate limiting"""
        elapsed = time.time() - self.last_request_time
        if elapsed < TRANSFERMARKT_DELAY:
            time.sleep(TRANSFERMARKT_DELAY - elapsed)
        self.last_request_time = time.time()

    def _get_soup(self, url: str) -> Optional[BeautifulSoup]:
        """URL'den BeautifulSoup objesi al"""
        self._rate_limit()

        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            if response.status_code == 200:
                return BeautifulSoup(response.content, 'lxml')
            logger.warning(f"Transfermarkt request failed: {response.status_code}")
            return None
        except Exception as e:
            logger.error(f"Transfermarkt error: {e}")
            return None

    def get_team_players(self, team_slug: str, team_id: str) -> List[Dict]:
        """Takım oyuncularını ve piyasa değerlerini al - detaylı kadro sayfasından"""
        # Detaylı kadro sayfası daha fazla bilgi içerir
        url = f"{self.base_url}/{team_slug}/kader/verein/{team_id}/saison_id/2024/plus/1"
        soup = self._get_soup(url)

        if not soup:
            # Fallback: ana sayfa
            url = f"{self.base_url}/{team_slug}/startseite/verein/{team_id}"
            soup = self._get_soup(url)
            if not soup:
                return []

        players = []
        table = soup.select_one('table.items')
        if not table:
            logger.warning(f"No table found for {team_slug}")
            return []

        for row in table.select('tbody tr.odd, tbody tr.even'):
            try:
                # Oyuncu adı ve linki
                player_link = row.select_one('td.hauptlink a')
                if not player_link:
                    continue

                name = player_link.text.strip()
                href = player_link.get('href', '')

                # Transfermarkt ID çıkar
                tm_id_match = re.search(r'/spieler/(\d+)', href)
                tm_id = tm_id_match.group(1) if tm_id_match else None

                # Piyasa değeri - sağdaki ana hücre
                value_cell = row.select_one('td.rechts.hauptlink')
                market_value = self._parse_market_value(value_cell.text if value_cell else '')

                # Tüm zentriert hücrelerini al
                centered_cells = row.select('td.zentriert')

                # Doğum tarihi ve yaş - genelde ilk veya ikinci hücrede
                age = None
                birth_date = None
                for cell in centered_cells:
                    cell_text = cell.text.strip()
                    # Doğum tarihi formatı: "01.01.2000 (24)"
                    age_match = re.search(r'\((\d{1,2})\)', cell_text)
                    if age_match:
                        age = int(age_match.group(1))
                        # Doğum tarihi
                        date_match = re.search(r'(\d{2}\.\d{2}\.\d{4})', cell_text)
                        if date_match:
                            birth_date = date_match.group(1)
                        break
                    # Sadece yaş varsa (bazı sayfalarda)
                    if cell_text.isdigit() and 15 <= int(cell_text) <= 45:
                        age = int(cell_text)
                        break

                # Pozisyon - inline-table içinde
                position = None
                pos_cell = row.select_one('td table.inline-table tr:last-child td')
                if pos_cell:
                    position = pos_cell.text.strip()

                # Alternatif pozisyon yeri
                if not position:
                    for cell in centered_cells:
                        cell_text = cell.text.strip()
                        if any(pos in cell_text.lower() for pos in ['kaleci', 'defans', 'orta', 'forvet', 'kanat', 'stoper', 'bek', 'santra']):
                            position = cell_text
                            break

                # Uyruk (nationality) - bayrak resmi varsa
                nationality = None
                flag_img = row.select_one('img.flaggenrahmen')
                if flag_img:
                    nationality = flag_img.get('title', '')

                players.append({
                    'name': name,
                    'transfermarkt_id': tm_id,
                    'market_value': market_value,
                    'age': age,
                    'birth_date': birth_date,
                    'position': position,
                    'nationality': nationality
                })

                logger.debug(f"Parsed: {name}, Age: {age}, Position: {position}, Value: {market_value}")

            except Exception as e:
                logger.debug(f"Row parse error: {e}")
                continue

        logger.info(f"Parsed {len(players)} players from {team_slug}")
        return players

    def get_player_market_value_history(self, player_slug: str, player_id: str) -> List[Dict]:
        """Oyuncu piyasa değeri geçmişi"""
        url = f"{self.base_url}/{player_slug}/marktwertverlauf/spieler/{player_id}"
        soup = self._get_soup(url)

        if not soup:
            return []

        # Grafik verisi genellikle JavaScript'te, basit scraping ile zor
        # Alternatif: Mevcut değeri al
        current_value = soup.select_one('a.data-header__market-value-wrapper')
        if current_value:
            value = self._parse_market_value(current_value.text)
            return [{'date': datetime.now().strftime('%Y-%m-%d'), 'value': value}]

        return []

    def _parse_market_value(self, text: str) -> Optional[float]:
        """Piyasa değeri metnini sayıya çevir"""
        if not text:
            return None

        text = text.strip().lower()

        # Milyon
        if 'mil' in text:
            match = re.search(r'([\d,\.]+)', text)
            if match:
                value = float(match.group(1).replace(',', '.'))
                return value * 1_000_000

        # Bin
        if 'bin' in text or 'k' in text:
            match = re.search(r'([\d,\.]+)', text)
            if match:
                value = float(match.group(1).replace(',', '.'))
                return value * 1_000

        return None


class FootballDataCollector:
    """Ana veri toplama sınıfı"""

    def __init__(self):
        self.supabase = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        self.api_football = APIFootballClient(API_FOOTBALL_KEY)
        self.transfermarkt = TransfermarktScraper()
        self.current_season = 2024
        self.stats = {
            'teams_fetched': 0,
            'players_fetched': 0,
            'stats_updated': 0,
            'values_updated': 0,
            'errors': 0
        }

    def log_scraping_start(self, source: str, entity_type: str) -> int:
        """Scraping log başlat"""
        log_data = {
            'source': source,
            'entity_type': entity_type,
            'status': 'running',
            'started_at': datetime.now().isoformat()
        }
        self.supabase.insert('scraping_logs', log_data)
        return 0  # Log ID'yi gerçek implementasyonda return et

    def log_scraping_end(self, log_id: int, records: int, status: str = 'completed'):
        """Scraping log bitir"""
        # Gerçek implementasyonda UPDATE yapılır
        pass

    def collect_teams(self, league_id: int) -> List[Dict]:
        """Lig takımlarını topla"""
        logger.info(f"Collecting teams for league {league_id}...")

        teams = self.api_football.get_teams(league_id, self.current_season)
        collected = []

        for team_data in teams:
            team = team_data.get('team', {})
            venue = team_data.get('venue', {})

            team_record = {
                'name': team.get('name'),
                'api_football_id': team.get('id'),
                'logo_url': team.get('logo'),
                'founded_year': team.get('founded'),
                'stadium': venue.get('name'),
                'city': venue.get('city'),
                'updated_at': datetime.now().isoformat()
            }

            if self.supabase.upsert('teams', team_record):
                collected.append(team_record)
                self.stats['teams_fetched'] += 1

        logger.info(f"Collected {len(collected)} teams")
        return collected

    def collect_league_players(self, league_id: int, max_pages: int = 10) -> List[Dict]:
        """Lig oyuncularını topla"""
        logger.info(f"Collecting players for league {league_id}...")

        all_players = []
        page = 1

        while page <= max_pages:
            result = self.api_football.get_league_players(league_id, self.current_season, page)
            players = result.get('players', [])
            paging = result.get('paging', {})

            if not players:
                break

            for player_data in players:
                player_info = player_data.get('player', {})
                stats_list = player_data.get('statistics', [])

                # Yaş hesapla
                birth_date = player_info.get('birth', {}).get('date')
                age = player_info.get('age')

                # Genç oyuncu mu? (23 yaş altı)
                is_youth = age and age < 23

                player_record = {
                    'name': player_info.get('name'),
                    'full_name': f"{player_info.get('firstname', '')} {player_info.get('lastname', '')}".strip(),
                    'birth_date': birth_date,
                    'age': age,
                    'height_cm': self._parse_height(player_info.get('height')),
                    'weight_kg': self._parse_weight(player_info.get('weight')),
                    'api_football_id': player_info.get('id'),
                    'photo_url': player_info.get('photo'),
                    'is_youth_player': is_youth,
                    'updated_at': datetime.now().isoformat()
                }

                # Pozisyon bilgisi stats'tan
                if stats_list:
                    games = stats_list[0].get('games', {})
                    player_record['primary_position'] = games.get('position')

                if self.supabase.upsert('players', player_record):
                    all_players.append(player_record)
                    self.stats['players_fetched'] += 1

                # İstatistikleri de kaydet
                self._save_player_stats(player_info.get('id'), stats_list)

            # Sonraki sayfa var mı?
            total_pages = paging.get('total', 1)
            logger.info(f"Page {page}/{total_pages} completed")

            if page >= total_pages:
                break

            page += 1

        logger.info(f"Collected {len(all_players)} players")
        return all_players

    def _save_player_stats(self, player_api_id: int, stats_list: List[Dict]):
        """Oyuncu istatistiklerini kaydet"""
        for stats in stats_list:
            team = stats.get('team', {})
            league = stats.get('league', {})
            games = stats.get('games', {})
            goals = stats.get('goals', {})
            passes = stats.get('passes', {})
            tackles = stats.get('tackles', {})
            duels = stats.get('duels', {})
            cards = stats.get('cards', {})

            # Sadece Türkiye ligleri
            if league.get('country') != 'Turkey':
                continue

            stat_record = {
                'appearances': games.get('appearences', 0),
                'starts': games.get('lineups', 0),
                'minutes_played': games.get('minutes', 0),
                'goals': goals.get('total', 0),
                'assists': goals.get('assists', 0),
                'yellow_cards': cards.get('yellow', 0),
                'red_cards': cards.get('red', 0),
                'shots': stats.get('shots', {}).get('total', 0),
                'shots_on_target': stats.get('shots', {}).get('on', 0),
                'pass_accuracy': passes.get('accuracy'),
                'key_passes': passes.get('key'),
                'tackles_won': tackles.get('total'),
                'interceptions': tackles.get('interceptions'),
                'average_rating': games.get('rating'),
                'updated_at': datetime.now().isoformat()
            }

            # Kaleci istatistikleri
            if games.get('position') == 'Goalkeeper':
                goal_stats = stats.get('goals', {})
                stat_record['goals_conceded'] = goal_stats.get('conceded', 0)
                stat_record['saves'] = goal_stats.get('saves', 0)

            self.stats['stats_updated'] += 1

    def collect_market_values(self, team_slug: str, team_tm_id: str):
        """Transfermarkt'tan piyasa değerlerini topla"""
        logger.info(f"Collecting market values for {team_slug}...")

        players = self.transfermarkt.get_team_players(team_slug, team_tm_id)

        for player in players:
            if player.get('market_value'):
                value_record = {
                    'recorded_at': datetime.now().strftime('%Y-%m-%d'),
                    'market_value': player['market_value'],
                    'source': 'transfermarkt'
                }
                self.stats['values_updated'] += 1

        logger.info(f"Collected {len(players)} player values")
        return players

    def collect_from_transfermarkt_only(self, team_slug: str, team_tm_id: str, team_name: str, league_id: int = 1):
        """Sadece Transfermarkt'tan oyuncu verisi topla ve Supabase'e kaydet"""
        logger.info(f"Collecting players from Transfermarkt: {team_name}...")

        players = self.transfermarkt.get_team_players(team_slug, team_tm_id)
        saved_count = 0

        if not players:
            logger.warning(f"No players found for {team_name}")
            return 0

        # Önce takımı kaydet
        team_record = {
            'name': team_name,
            'short_name': team_name[:20] if len(team_name) > 20 else team_name,
            'transfermarkt_id': team_tm_id,
            'league_id': league_id,
            'updated_at': datetime.now().isoformat()
        }
        self.supabase.upsert('teams', team_record)

        # Takım ID'sini al
        teams = self.supabase.select('teams', {'transfermarkt_id': f'eq.{team_tm_id}'})
        team_db_id = teams[0]['id'] if teams else None

        for player in players:
            try:
                # Pozisyonu İngilizce'ye çevir
                position = self._translate_position(player.get('position', ''))

                # Genç oyuncu mu? (23 yaş altı)
                age = player.get('age')
                is_youth = bool(age and age < 23)

                # Doğum tarihini parse et (DD.MM.YYYY -> YYYY-MM-DD)
                birth_date = None
                if player.get('birth_date'):
                    try:
                        parts = player['birth_date'].split('.')
                        if len(parts) == 3:
                            birth_date = f"{parts[2]}-{parts[1]}-{parts[0]}"
                    except:
                        pass

                player_record = {
                    'name': player.get('name'),
                    'transfermarkt_id': player.get('transfermarkt_id'),
                    'age': age,
                    'birth_date': birth_date,
                    'primary_position': position,
                    'is_youth_player': is_youth,
                    'nationality_id': 1,  # Türkiye (varsayılan)
                    'updated_at': datetime.now().isoformat()
                }

                logger.info(f"Saving: {player.get('name')}, Age: {age}, Position: {position}, Youth: {is_youth}")

                # Oyuncuyu kaydet
                if self.supabase.upsert('players', player_record):
                    saved_count += 1
                    self.stats['players_fetched'] += 1

                    # Oyuncu ID'sini al
                    players_db = self.supabase.select('players', {'transfermarkt_id': f"eq.{player.get('transfermarkt_id')}"})
                    if players_db:
                        player_db_id = players_db[0]['id']

                        # Piyasa değerini kaydet
                        if player.get('market_value'):
                            value_record = {
                                'player_id': player_db_id,
                                'recorded_at': datetime.now().strftime('%Y-%m-%d'),
                                'market_value': player['market_value'],
                                'source': 'transfermarkt'
                            }
                            self.supabase.upsert('player_market_values', value_record)
                            self.stats['values_updated'] += 1

                        # player_teams ilişkisi
                        if team_db_id:
                            team_relation = {
                                'player_id': player_db_id,
                                'team_id': team_db_id,
                                'season_id': 1,  # 2024-2025
                                'is_current': True
                            }
                            self.supabase.upsert('player_teams', team_relation)

            except Exception as e:
                logger.error(f"Error saving player {player.get('name')}: {e}")
                self.stats['errors'] += 1

        logger.info(f"Saved {saved_count} players from {team_name}")
        return saved_count

    def _translate_position(self, position_tr: str) -> str:
        """Türkçe pozisyonu İngilizce'ye çevir"""
        if not position_tr:
            return 'Unknown'

        position_tr = position_tr.lower().strip()

        # Kaleci
        if any(x in position_tr for x in ['kaleci', 'goalkeeper', 'torwart', 'tw']):
            return 'Goalkeeper'

        # Defans - çeşitli pozisyonlar
        if any(x in position_tr for x in [
            'stoper', 'defans', 'bek', 'savunma',
            'sol bek', 'sağ bek', 'libero',
            'innenverteidiger', 'verteidiger',
            'linker verteidiger', 'rechter verteidiger',
            'iv', 'lv', 'rv', 'lb', 'rb'
        ]):
            return 'Defender'

        # Orta saha - çeşitli pozisyonlar
        if any(x in position_tr for x in [
            'orta saha', 'ortasaha', 'merkez', 'oyun kurucu',
            'defansif orta', 'ofansif orta', 'box-to-box',
            'mittelfeld', 'zentrales mittelfeld',
            'defensives mittelfeld', 'offensives mittelfeld',
            'dm', 'om', 'zm', 'cm', 'lm', 'rm',
            'sol orta', 'sağ orta', 'on numara'
        ]):
            return 'Midfielder'

        # Forvet / Santrafor - çeşitli pozisyonlar
        if any(x in position_tr for x in [
            'forvet', 'santrafor', 'kanat', 'hücum', 'golcü',
            'sol kanat', 'sağ kanat', 'ikinci forvet',
            'stürmer', 'mittelstürmer', 'linksaußen', 'rechtsaußen',
            'hängende spitze', 'flügel',
            'st', 'cf', 'lw', 'rw', 'ss', 'rf', 'lf'
        ]):
            return 'Attacker'

        # Bilinmeyen ama debug için logla
        logger.debug(f"Unknown position: {position_tr}")
        return 'Unknown'

    def run_transfermarkt_collection(self, teams: dict = None):
        """Sadece Transfermarkt'tan veri toplama (API olmadan)"""
        if teams is None:
            teams = SUPER_LIG_TEAMS_TM

        logger.info("=" * 60)
        logger.info("TRANSFERMARKT VERİ TOPLAMA BAŞLADI (API-FREE)")
        logger.info("=" * 60)

        start_time = time.time()

        for team_slug, team_id in teams.items():
            team_name = team_slug.replace('-', ' ').title().replace('Istanbul', 'İstanbul')
            team_name = team_name.replace('Fk', 'FK').replace('Sk', 'SK')

            try:
                self.collect_from_transfermarkt_only(team_slug, team_id, team_name)
                time.sleep(TRANSFERMARKT_DELAY)
            except Exception as e:
                logger.error(f"Error collecting {team_name}: {e}")
                self.stats['errors'] += 1

        elapsed = time.time() - start_time

        logger.info("\n" + "=" * 60)
        logger.info("VERİ TOPLAMA TAMAMLANDI")
        logger.info(f"Süre: {elapsed/60:.1f} dakika")
        logger.info(f"Oyuncu: {self.stats['players_fetched']}")
        logger.info(f"Piyasa Değeri: {self.stats['values_updated']}")
        logger.info(f"Hata: {self.stats['errors']}")
        logger.info("=" * 60)

    def collect_youth_players(self, min_age: int = 16, max_age: int = 21):
        """Genç yetenekleri topla (tüm liglerden)"""
        logger.info(f"Collecting youth players (age {min_age}-{max_age})...")

        youth_players = []

        for league_id in TURKISH_LEAGUES.keys():
            logger.info(f"Scanning league {league_id} for youth players...")

            page = 1
            while page <= 5:  # Her lig için max 5 sayfa
                result = self.api_football.get_league_players(league_id, self.current_season, page)
                players = result.get('players', [])

                if not players:
                    break

                for player_data in players:
                    player_info = player_data.get('player', {})
                    age = player_info.get('age')

                    if age and min_age <= age <= max_age:
                        youth_players.append({
                            'name': player_info.get('name'),
                            'age': age,
                            'api_id': player_info.get('id'),
                            'league_id': league_id,
                            'league_name': TURKISH_LEAGUES[league_id]['name']
                        })

                page += 1

        logger.info(f"Found {len(youth_players)} youth players")
        return youth_players

    def _parse_height(self, height_str: str) -> Optional[int]:
        """Boy stringini cm'ye çevir"""
        if not height_str:
            return None
        match = re.search(r'(\d+)', height_str)
        return int(match.group(1)) if match else None

    def _parse_weight(self, weight_str: str) -> Optional[int]:
        """Kilo stringini kg'ye çevir"""
        if not weight_str:
            return None
        match = re.search(r'(\d+)', weight_str)
        return int(match.group(1)) if match else None

    def run_full_collection(self, leagues: List[int] = None):
        """Tam veri toplama"""
        if leagues is None:
            leagues = list(TURKISH_LEAGUES.keys())

        logger.info("=" * 60)
        logger.info("TÜRK FUTBOLU VERİ TOPLAMA BAŞLADI")
        logger.info("=" * 60)

        start_time = time.time()

        for league_id in leagues:
            league_name = TURKISH_LEAGUES.get(league_id, {}).get('name', f'League {league_id}')
            logger.info(f"\n--- {league_name} ---")

            try:
                # Takımları topla
                self.collect_teams(league_id)

                # Oyuncuları topla
                self.collect_league_players(league_id)

            except Exception as e:
                logger.error(f"Error in league {league_id}: {e}")
                self.stats['errors'] += 1

        elapsed = time.time() - start_time

        logger.info("\n" + "=" * 60)
        logger.info("VERİ TOPLAMA TAMAMLANDI")
        logger.info(f"Süre: {elapsed/60:.1f} dakika")
        logger.info(f"Takım: {self.stats['teams_fetched']}")
        logger.info(f"Oyuncu: {self.stats['players_fetched']}")
        logger.info(f"İstatistik: {self.stats['stats_updated']}")
        logger.info(f"Piyasa Değeri: {self.stats['values_updated']}")
        logger.info(f"Hata: {self.stats['errors']}")
        logger.info("=" * 60)


# Transfermarkt Takım Slug'ları (manuel mapping gerekli)
SUPER_LIG_TEAMS_TM = {
    'galatasaray-istanbul': '141',
    'fenerbahce-istanbul': '36',
    'besiktas-istanbul': '114',
    'trabzonspor': '449',
    'basaksehir-fk': '6890',
    'adana-demirspor': '3085',
    'konyaspor': '2384',
    'kayserispor': '3205',
    'antalyaspor': '589',
    'sivasspor': '2387',
    'alanyaspor': '10484',
    'kasimpasa-sk': '2948',
    'samsunspor': '162',
    'pendikspor': '64417',
    'fatih-karagumruk': '3859',
    'rizespor': '126',
    'istanbulspor': '3061',
    'hatayspor': '7179',
    'ankaragucumetut-sk': '134',
    'gaziantep-fk': '2832'
}


def main():
    """Ana fonksiyon - Sadece Transfermarkt (API gerektirmez)"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("SUPABASE_URL and SUPABASE_KEY environment variables required!")
        return

    collector = FootballDataCollector()

    # Sadece Transfermarkt'tan veri topla (API-Football gerektirmez)
    collector.run_transfermarkt_collection()


if __name__ == '__main__':
    main()
