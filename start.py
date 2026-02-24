#!/usr/bin/env python3
"""
Pinterest Bot Ultimate: Scraper + Telegram Bot + Flask API + SQLite Caching
Все в одном файле.

Функционал:
1. Ищет картинки на Pinterest (без API, парсинг).
2. Кэширует file_id телеграма в SQLite.
3. При повторном поиске отдает готовые file_id (мгновенная отправка).
4. Запускает Flask сервер для внешних запросов.
"""

import sys
import os
import time
import json
import re
import random
import logging
import sqlite3
import threading
import requests
import gzip
import signal
from io import BytesIO
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

# Сторонние библиотеки (убедитесь, что установлены: pip install pyTelegramBotAPI flask requests beautifulsoup4)
import telebot
from telebot import types
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup

# --- КОНФИГУРАЦИЯ ---

# Токен бота
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Настройки сервера
FLASK_PORT = 5091
FLASK_HOST = "0.0.0.0"

# Файл базы данных
DB_FILE = "pinterest_cache.db"

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("PinterestBot")

# Константы для Scraper
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
]

USER_COOKIES = [
    # Установите свои Pinterest cookies для авторизации
    # {"name": "_pinterest_sess", "value": "YOUR_PINTEREST_SESSION_HERE", "domain": ".pinterest.com"},
    # {"name": "__Secure-s_a", "value": "YOUR_SECURE_SA_HERE", "domain": ".pinterest.com"},
    # {"name": "csrftoken", "value": "YOUR_CSRF_TOKEN_HERE", "domain": "ru.pinterest.com"},
]

# --- 1. КЭШ И БАЗА ДАННЫХ (SQLite) ---

class CacheManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _init_db(self):
        """Создает таблицы, если их нет"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            # Таблица для хранения URL -> FileID
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS images (
                url_hash TEXT PRIMARY KEY,
                original_url TEXT,
                file_id TEXT,
                media_type TEXT DEFAULT 'photo',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            # Таблица для хранения поисковых запросов (опционально, чтобы не парсить часто)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS searches (
                query TEXT PRIMARY KEY,
                results_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.commit()

    def normalize_url(self, url):
        """
        Нормализация ссылки:
        https://site.com/img.jpg?utm_source=... -> https://site.com/img.jpg
        """
        try:
            parsed = urlparse(url)
            # Возвращаем схему + нетлок + путь. Отсекаем query params (?, &)
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            return clean_url
        except:
            return url

    def get_file_id(self, url):
        """Возвращает file_id, если он есть в базе"""
        clean_url = self.normalize_url(url)
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT file_id FROM images WHERE url_hash = ?", (clean_url,))
            row = cursor.fetchone()
            return row[0] if row else None

    def save_file_id(self, url, file_id, media_type='photo'):
        """Сохраняет file_id для нормализованной ссылки"""
        clean_url = self.normalize_url(url)
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                INSERT OR REPLACE INTO images (url_hash, original_url, file_id, media_type)
                VALUES (?, ?, ?, ?)
                """, (clean_url, url, file_id, media_type))
                conn.commit()
            logger.info(f"✅ Cached file_id for: {clean_url}")
        except Exception as e:
            logger.error(f"DB Save error: {e}")

    def get_search_results(self, query):
        """Возвращает сохраненные результаты поиска"""
        normalized_query = query.lower().strip()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT results_json FROM searches WHERE query = ?", (normalized_query,))
            row = cursor.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except:
                    return None
            return None

    def save_search_results(self, query, results):
        """Сохраняет результаты поиска"""
        normalized_query = query.lower().strip()
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                INSERT OR REPLACE INTO searches (query, results_json)
                VALUES (?, ?)
                """, (normalized_query, json.dumps(results)))
                conn.commit()
                logger.info(f"✅ Cached search results for: {normalized_query}")
        except Exception as e:
            logger.error(f"Search cache save error: {e}")

# Инициализация менеджера кэша
cache = CacheManager(DB_FILE)

# --- 2. PINTEREST SCRAPER (Логика парсинга) ---

class PinterestScraper:
    def __init__(self):
        self.session = requests.Session()
        self._init_session()

    def _init_session(self):
        self.session.cookies.clear()
        for cookie in USER_COOKIES:
            self.session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])
        
        self.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
            "User-Agent": random.choice(USER_AGENTS)
        }

    def _make_request(self, url, params=None, retries=3, timeout=10):
        """
        Выполняет HTTP-запрос с ротацией UA, обработкой кук и ретраями.
        """
        headers = self.headers.copy()
        headers["User-Agent"] = random.choice(USER_AGENTS)
        
        # Отключаем автоматическую декомпрессию
        headers.pop("Accept-Encoding", None)

        attempt = 0
        while attempt < retries:
            try:
                # Случайная задержка
                time.sleep(random.uniform(1.0, 2.5))
                
                logger.info(f"Requesting {url} (Attempt {attempt+1}/{retries})")
                resp = self.session.get(url, headers=headers, params=params, timeout=timeout, stream=False)
                
                # Если нас детектят как бота (слишком короткий ответ), пробуем переинициализировать сессию
                if resp.status_code == 200 and len(resp.text) < 5000:
                    logger.warning(f"Response too short ({len(resp.text)} chars). Possible bot detection.")
                    self._init_session()
                    headers["User-Agent"] = random.choice(USER_AGENTS)
                    attempt += 1
                    continue
                
                # Проверяем и декомпрессируем gzip если нужно
                if resp.headers.get('content-encoding') == 'gzip':
                    try:
                        # Декомпрессируем контент
                        compressed_data = BytesIO(resp.content)
                        decompressed_data = gzip.GzipFile(fileobj=compressed_data).read().decode('utf-8')
                        logger.info(f"Decompressed gzip content: {len(decompressed_data)} chars")
                        resp._content = decompressed_data.encode('utf-8')
                    except Exception as e:
                        logger.warning(f"Failed to decompress gzip: {e}")

                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    logger.warning(f"Got status {resp.status_code}. Retrying...")
                    attempt += 1
                    time.sleep(2 ** attempt)
                    continue
                    
                resp.raise_for_status()
                return resp.text

            except Exception as e:
                logger.error(f"Request failed: {e}")
                attempt += 1
                time.sleep(2 ** attempt)

        logger.error("All retries failed.")
        return None

    def search(self, query, limit=20):
        """Парсинг выдачи Pinterest"""
        base_url = "https://ru.pinterest.com/search/pins/"
        html = self._make_request(base_url, params={"q": query, "rs": "typed"})
        
        if not html:
            return []

        return self._parse_images_from_html(html, limit)

    def download_by_url(self, url):
        """Скачивает изображение по прямой ссылке Pinterest"""
        if not url or 'pinimg.com' not in url:
            return None
            
        # Проверяем кэш
        cached_file_id = cache.get_file_id(url)
        if cached_file_id:
            logger.info(f"📁 Found cached file_id for URL: {url}")
            return [{'src': url, 'alt': 'Pinterest Image', 'file_id': cached_file_id}]
        
        # Проверяем доступность ссылки
        try:
            response = self.session.head(url, headers=self.headers, timeout=10)
            if response.status_code != 200:
                logger.warning(f"URL not accessible: {url} (status: {response.status_code})")
                return None
                
            content_type = response.headers.get('content-type', '')
            if not content_type.startswith('image/'):
                logger.warning(f"URL is not an image: {url} (content-type: {content_type})")
                return None
                
            logger.info(f"✅ URL is valid image: {url}")
            return [{'src': url, 'alt': 'Pinterest Image'}]
            
        except Exception as e:
            logger.error(f"Error checking URL {url}: {e}")
            return None

    def _parse_images_from_html(self, html_content, limit=None):
        """
        Парсит HTML с помощью BS4, ищет картинки с доменом pinimg.com.
        Также заглядывает в JSON данные (SSR), если в HTML мало картинок.
        """
        images = []
        seen_urls = set()
        
        logger.info(f"HTML content length: {len(html_content)}")
        
        # 1. Попытка извлечь из JSON (самый надежный способ для SSR)
        # Ищем любые блоки JSON, так как Pinterest часто меняет ID
        script_tags = re.findall(r'<script [^>]*type="application/json"[^>]*>(.*?)</script>', html_content, re.S)
        logger.info(f"Found {len(script_tags)} JSON blocks in HTML")
        
        # Также ищем данные в window.__PWS__ и других начальных данных
        pws_data = re.findall(r'window\.__PWS__\s*=\s*({.+?});', html_content)
        logger.info(f"Found {len(pws_data)} PWS data blocks")
        
        # Ищем другие скрипты с данными
        initial_data = re.findall(r'window\.__INITIAL_DATA__\s*=\s*({.+?});', html_content)
        logger.info(f"Found {len(initial_data)} INITIAL data blocks")
        
        all_json_blocks = script_tags.copy()
        
        # Добавляем найденные данные
        for data_text in pws_data + initial_data:
            try:
                all_json_blocks.append(data_text)
            except:
                pass
        
        for json_text in all_json_blocks:
            try:
                data = json.loads(json_text)
                self._find_pins(data, images, seen_urls)
            except Exception:
                continue

        # 2. Если JSON не дал результатов, пробуем регулярные выражения по всему тексту
        if not images:
            logger.info("Falling back to regex extraction...")
            # Ищем все ссылки на картинки Pinterest
            raw_urls = re.findall(r'https://i\.pinimg\.com/[^"\'\s<>]+', html_content)
            logger.info(f"Found {len(raw_urls)} raw image URLs with regex")
            for url in raw_urls:
                clean_url = url.split(' ')[0].split('"')[0].split("'")[0]
                if clean_url not in seen_urls and any(x in clean_url for x in ['/originals/', '/736x/', '/564x/', '/474x/', '/236x/']):
                    if '75x75' not in clean_url and 'custom' not in clean_url:
                        seen_urls.add(clean_url)
                        images.append({'src': clean_url, 'alt': 'Pinterest Image'})

        # 3. BeautifulSoup (последний рубеж)
        if not images:
            logger.info("Falling back to BeautifulSoup...")
            soup = BeautifulSoup(html_content, 'html.parser')
            img_tags = soup.find_all('img')
            logger.info(f"Found {len(img_tags)} img tags with BeautifulSoup")
            for img in img_tags:
                src = img.get('src') or img.get('data-src') or img.get('data-original')
                if src and 'pinimg.com' in src and '75x75' not in src and 'custom' not in src:
                    if src not in seen_urls:
                        seen_urls.add(src)
                        images.append({'src': src, 'alt': img.get('alt', 'Pinterest Image')})

        logger.info(f"Final images count: {len(images)}")
        return images[:limit] if limit else images

    def _find_pins(self, obj, images, seen):
        """Рекурсивный поиск пинов в JSON структуре"""
        if isinstance(obj, dict):
            # Ищем структуру с картинками
            if 'images' in obj and isinstance(obj['images'], dict):
                img_variants = obj['images']
                best_url = None
                for key in ['originals', '736x', '564x', '474x', '236x']:
                    v = img_variants.get(key)
                    if isinstance(v, dict) and v.get('url'):
                        best_url = v['url']
                        break
                
                if best_url and best_url not in seen:
                    seen.add(best_url)
                    images.append({
                        'src': best_url, 
                        'alt': obj.get('description') or obj.get('title') or 'Pinterest Image'
                    })
            
            # Рекурсивно идем дальше
            for value in obj.values():
                self._find_pins(value, images, seen)
        elif isinstance(obj, list):
            for item in obj:
                self._find_pins(item, images, seen)

scraper = PinterestScraper()

# --- 3. TELEGRAM BOT (Логика бота и отправки) ---

bot = telebot.TeleBot(BOT_TOKEN)
executor = ThreadPoolExecutor(max_workers=5)

# Add error handling for bot conflicts
bot.set_update_listener(lambda messages: None)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    help_text = """<b>Pinterest Super Bot</b>

🔍 <b>Поиск:</b>
Просто напиши запрос: <code>котики</code>, <code>архитектура</code>, <code>мемы</code>

🔗 <b>Ссылки:</b>
Отправь прямую ссылку на Pinterest изображение (pinimg.com)

⚡ <b>Инлайн:</b>
В любом чате: <code>@pintorobot котики</code> или <code>@pintorobot [ссылка]</code>

💾 <b>Кэш:</b>
Я запоминаю отправленные файлы. Второй раз картинка приходит мгновенно (по file_id).
"""
    bot.reply_to(message, help_text, parse_mode='HTML')

@bot.message_handler(func=lambda message: True)
def handle_text_search(message):
    """Обработка текстового запроса в чате"""
    query = message.text.strip()
    if not query or query.startswith('/'): return

    status_msg = bot.reply_to(message, f"🔍 Ищу <b>{query}</b>...", parse_mode='HTML')

    try:
        images = []
        
        # 1. Проверяем если это ссылка на Pinterest изображение
        if 'pinimg.com' in query:
            logger.info(f"🔗 Detected Pinterest image URL: {query}")
            bot.edit_message_text("🔗 Обнаружена ссылка на Pinterest...", message.chat.id, status_msg.message_id)
            
            result = scraper.download_by_url(query)
            if result:
                images = result
                bot.edit_message_text("✅ Ссылка подтверждена", message.chat.id, status_msg.message_id)
            else:
                bot.edit_message_text("❌ Не удалось загрузить изображение по ссылке", message.chat.id, status_msg.message_id)
                return
        else:
            # 2. Проверяем кэш поисковых запросов
            cached_images = cache.get_search_results(query)
            
            if cached_images:
                logger.info(f"🎯 Cache hit for search query: {query}")
                images = cached_images
                bot.edit_message_text(f"⚡ Найдено в кэше: {len(images)} изображений", message.chat.id, status_msg.message_id)
            else:
                # 3. Если нет в кэше, ищем на Pinterest
                images = scraper.search(query, limit=10)
                
                if not images:
                    bot.edit_message_text("😔 Ничего не найдено.", message.chat.id, status_msg.message_id)
                    return
                
                # Сохраняем результаты поиска в кэш
                cache.save_search_results(query, images)
                bot.edit_message_text(f"🔍 Найдено: {len(images)} изображений", message.chat.id, status_msg.message_id)

        # 4. Формирование медиа-группы
        media_group = []
        url_map = {} # Индекс -> URL для сохранения file_id

        for idx, img in enumerate(images):
            url = img['src']
            
            # ПРОВЕРКА КЭША file_id
            cached_file_id = cache.get_file_id(url)
            
            if cached_file_id or img.get('file_id'):
                # Если есть в кэше или уже загружен, используем file_id
                file_id_to_use = cached_file_id or img.get('file_id')
                media = types.InputMediaPhoto(media=file_id_to_use)
                logger.info(f"📁 File ID cache hit for: {url}")
            else:
                # Если нет, используем URL (Telegram сам скачает)
                media = types.InputMediaPhoto(media=url)
                # Запоминаем, что этот индекс нужно будет закэшировать
                url_map[idx] = url
            
            if idx == 0:
                if 'pinimg.com' in query:
                    media.caption = f"🔗 Pinterest изображение"
                else:
                    media.caption = f"🎨 Результаты: <b>{query}</b>"
                media.parse_mode = 'HTML'
            
            media_group.append(media)

        # 5. Отправка и "Самообучение" (Harvesting file_ids)
        if media_group:
            bot.delete_message(message.chat.id, status_msg.message_id)
            
            # Отправляем группу
            sent_messages = bot.send_media_group(message.chat.id, media_group)
            
            # 6. Сохранение полученных file_id в БД
            for i, msg in enumerate(sent_messages):
                if i in url_map and msg.photo:
                    # Берем самый большой вариант фото
                    file_id = msg.photo[-1].file_id
                    original_url = url_map[i]
                    # Сохраняем в базу
                    cache.save_file_id(original_url, file_id)

    except Exception as e:
        logger.error(f"Error handling message: {e}")
        bot.reply_to(message, "Ошибка при поиске. Попробуйте позже.")

@bot.inline_handler(lambda query: len(query.query) > 0)
def handle_inline(inline_query):
    """Инлайн поиск"""
    try:
        query = inline_query.query.strip()
        
        # Проверяем если это ссылка на Pinterest изображение
        if 'pinimg.com' in query:
            logger.info(f"🔗 Inline detected Pinterest URL: {query}")
            
            result = scraper.download_by_url(query)
            if result:
                images = result
            else:
                # Если ссылка не работает, возвращаем пустой результат
                bot.answer_inline_query(inline_query.id, [])
                return
        else:
            # Проверяем кэш поисковых запросов
            cached_images = cache.get_search_results(query)
            
            if cached_images:
                logger.info(f"🎯 Inline cache hit for: {query}")
                images = cached_images
            else:
                # Если нет в кэше, ищем на Pinterest
                images = scraper.search(query, limit=20)
                if images:
                    cache.save_search_results(query, images)
        
        results = []
        for i, img in enumerate(images):
            url = img['src']
            cached_id = cache.get_file_id(url)
            
            result_id = f"{inline_query.id}_{i}"
            
            if cached_id or img.get('file_id'):
                # Если есть ID, используем CachedPhoto (быстрее и надежнее)
                file_id_to_use = cached_id or img.get('file_id')
                results.append(types.InlineQueryResultCachedPhoto(
                    id=result_id,
                    photo_file_id=file_id_to_use,
                    title=f"Pinterest {i+1}"
                ))
            else:
                # Если нет, используем URL
                results.append(types.InlineQueryResultPhoto(
                    id=result_id,
                    photo_url=url,
                    thumbnail_url=url,
                    title=f"Pinterest {i+1}"
                ))
        
        bot.answer_inline_query(inline_query.id, results, cache_time=300)
        
    except Exception as e:
        logger.error(f"Inline error: {e}")

# --- 4. FLASK API (Веб-сервер) ---

app = Flask(__name__)

@app.route('/search', methods=['GET'])
def api_search():
    """API endpoint для поиска"""
    query = request.args.get('q')
    limit = request.args.get('limit', default=10, type=int)
    
    if not query:
        return jsonify({"error": "No query provided"}), 400
        
    data = scraper.search(query, limit)
    
    # Обогащаем ответ информацией о кэше
    for item in data:
        item['cached'] = bool(cache.get_file_id(item['src']))
        
    return jsonify({"count": len(data), "results": data})

@app.route('/', methods=['GET'])
def index():
    return "Pinterest Bot & API is running. Telegram Bot is active."

# --- 5. ЗАПУСК ВСЕГО (Threading) ---

def run_flask():
    print(f"Flask API running on http://{FLASK_HOST}:{FLASK_PORT}")
    # use_reloader=False важно, чтобы не создавать дубликаты процессов
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False)

def run_bot():
    print("Telegram Bot started...")
    try:
        bot.polling(none_stop=True)
    except Exception as e:
        logger.error(f"Bot polling error: {e}")
        time.sleep(5)  # Wait before retry
        run_bot()  # Retry polling

def signal_handler(sig, frame):
    print("\nShutting down...")
    os._exit(0)

if __name__ == '__main__':
    # Обработка Ctrl+C
    signal.signal(signal.SIGINT, signal_handler)
    
    print("="*40)
    print("🎨 STARTED: Pinterest Bot + Cache + API")
    print("="*40)

    # Запускаем Flask в отдельном потоке
    t_flask = threading.Thread(target=run_flask, daemon=True)
    t_flask.start()
    
    # Даем фору на старт
    time.sleep(1)
    
    # Запускаем бота в основном потоке (или отдельном, если нужно еще что-то делать)
    try:
        run_bot()
    except KeyboardInterrupt:
        pass