import redis
import json
import requests
import time
import logging
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from bs4.element import NavigableString
from markdownify import MarkdownConverter
from dotenv import load_dotenv
import os

# .env 파일 로드
load_dotenv()

# 환경 변수 가져오기 (기본값 없음, 없으면 예외 발생)
def get_env_var(key):
    value = os.getenv(key)
    if value is None:
        raise EnvironmentError(f"Required environment variable '{key}' is missing.")
    return value

# 필수 환경 변수 가져오기
REDIS_HOST = get_env_var("REDIS_HOST")
REDIS_PORT = int(get_env_var("REDIS_PORT"))
REDIS_DB = int(get_env_var("REDIS_DB"))
REDIS_PASSWORD = get_env_var("REDIS_PASSWORD")  # 비밀번호가 비어있을 수 있다면 기본값 None 대신 빈 문자열 허용
CHROME_DRIVER_PATH = get_env_var("CHROME_DRIVER_PATH")
BASE_URL = get_env_var("BASE_URL")
LOG_LEVEL = get_env_var("LOG_LEVEL")

# 로그 설정
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Redis 설정
redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    password=REDIS_PASSWORD,
    decode_responses=True,
)

# 카테고리 맵
CATEGORY_MAP = {
    1: "100",  # 정치
    2: "101",  # 경제
    3: "102",  # 사회
    4: "103",  # 생활/문화
    5: "104",  # 세계
    6: "105",  # IT/과학
}

# HTML to Markdown 
class CustomMarkdownConverter(MarkdownConverter):
    def convert_div(self, el, text, convert_as_inline):
        style = el.get("style", "")
        if "font-weight: 700" in style or "font-size: 18px" in style:
            return f"## {text}  "
        return f"{text}  "

    def convert_img(self, el, text, convert_as_inline):
        img_url = el.get("data-src") or el.get("src", "")
        alt_text = el.get("alt", "이미지")
        if img_url:
            return f"![{alt_text}]({img_url})  "
        return ""

    def convert_text(self, el, text, convert_as_inline):
        text = super().convert_text(el, text, convert_as_inline)
        return text.replace("\n", "  ")

def convert_content_to_markdown(content_html):
    try:
        converter = CustomMarkdownConverter()
        markdown = converter.convert(content_html)
        return markdown
    except Exception as e:
        logger.error(f"HTML을 Markdown으로 변환 중 오류가 발생했습니다.: {e}")
        return ""

# 기사 내용 및 발행일 크롤링
def fetch_article_content_and_published_date(article_url):
    try:
        response = requests.get(article_url, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")

        published_date_tag = soup.find("span", class_="media_end_head_info_datestamp_time _ARTICLE_DATE_TIME")
        published_date = published_date_tag["data-date-time"] if published_date_tag else "발행 날짜를 찾을 수 없습니다."

        content_html = soup.select_one("#dic_area")
        content_html = str(content_html) if content_html else "본문을 가져올 수 없습니다."

        return content_html, published_date
    except Exception as e:
        logging.error(f"기사 내용을 가져오는 데 오류가 생겼습니다다: {e}")
        return "본문을 가져올 수 없습니다.", "발행 날짜를 찾을 수 없습니다."

# RedisArticle 형식으로 Redis에 저장
def save_articles_to_redis(category_id, articles):
    try:
        redis_key = f"articles:category:{category_id}"
        existing_articles = redis_client.get(redis_key)

        if existing_articles:
            articles_list = json.loads(existing_articles)
        else:
            articles_list = []

        for article in articles:
            article_id = redis_client.incr("article_id")
            
            redis_article = {
                "articleId": article_id,
                "title": article["title"],
                "source": article["source"],
                "publishedDate": article["publishedDate"],
                "thumbnail": article.get("thumbnail", ""),  
                "content": article["content"]
            }

            articles_list.append(redis_article)

        now = datetime.now()
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        seconds_until_midnight = int((midnight - now).total_seconds())
        
        redis_client.setex(redis_key, seconds_until_midnight, json.dumps(articles_list, ensure_ascii=False))

        logging.info(f"카테고리 {category_id}번의 {len(articles)}개 최신 뉴스를 Redis에 저장했습니다.")
    except Exception as e:
        logging.error(f"기사를 Redis에 저장하는데 오류가 생겼습니다.: {e}")

# 카테고리별 기사 크롤링
def fetch_recent_articles_by_category(category_id):
    naver_category_id = CATEGORY_MAP.get(category_id)
    if not naver_category_id:
        raise ValueError(f"Invalid categoryId: {category_id}")

    articles, seen_urls = [], set()
    options = webdriver.ChromeOptions()

    options.add_argument('--headless')  
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    driver = webdriver.Chrome(service=Service(CHROME_DRIVER_PATH), options=options)

    try:
        driver.get(f"{BASE_URL}/{naver_category_id}")

        # 기사 더보기 버튼 10번 클릭
        for _ in range(10):
            try:
                button = driver.find_element(By.CLASS_NAME, "_CONTENT_LIST_LOAD_MORE_BUTTON")
                button.click()
                time.sleep(1)
            except NoSuchElementException:
                logging.info("더보기 버튼이 없습니다. 종료합니다.")
                break

        # 현재 페이지의 기사 가져오기
        soup = BeautifulSoup(driver.page_source, "lxml")
        articles_items = soup.select(".section_latest .sa_item")
        current_page_no = int(soup.select_one(".section_latest_article")["data-page-no"])

        for item in articles_items:
            title_tag = item.select_one(".sa_text_title")
            link = title_tag["href"] if title_tag else None
            press_tag = item.select_one(".sa_text_press")
            
            thumbnail_tag = item.select_one(".sa_thumb_inner img")
            thumbnail_url = thumbnail_tag["src"] if thumbnail_tag else None

            if link and link not in seen_urls:
                seen_urls.add(link)
                content_html, published_date = fetch_article_content_and_published_date(link)

                # 8시간 이내 기사 필터링
                if datetime.now() - datetime.strptime(published_date, "%Y-%m-%d %H:%M:%S") <= timedelta(hours=8):
                    articles.append({
                        "title": title_tag.get_text(strip=True),
                        "source": press_tag.get_text(strip=True) if press_tag else "알 수 없음",
                        "thumbnail": thumbnail_url, 
                        "content": convert_content_to_markdown(content_html),
                        "publishedDate": published_date
                    })
    finally:
        driver.quit()

    save_articles_to_redis(category_id, articles)
    return articles


# 전체 카테고리 크롤링
def crawl_recent_articles():
    for category_id in CATEGORY_MAP:
        articles = fetch_recent_articles_by_category(category_id)
