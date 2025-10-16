import os
import sqlite3
import json
import re
import time
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from googleapiclient.discovery import build
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
from dotenv import load_dotenv
import logging
import sys
from threading import Lock

# PostgreSQL 연결
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

if sys.platform.startswith('win'):
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            # stdout/stderr에 쓰는 핸들러의 인코딩을 강제로 utf-8로 설정
            handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

load_dotenv()

# --- 설정 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "1"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "recipes.db")
DATABASE_URL = os.getenv("DATABASE_URL")
FREE_TIER_LIMIT = 10

if not GEMINI_API_KEY or not YOUTUBE_API_KEY:
    logger.error("API 키가 설정되지 않았습니다.")
    raise ValueError("API keys not configured")

genai.configure(api_key=GEMINI_API_KEY)
youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24))

processing_status = {}
status_lock = Lock()

# --- 데이터베이스 ---
def get_db_connection():
    if DATABASE_URL and 'postgres' in DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    else:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def init_database():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if DATABASE_URL and 'postgres' in DATABASE_URL:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS recipes (
                    id SERIAL PRIMARY KEY,
                    video_id TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    ingredients TEXT,
                    dish_name TEXT,
                    url TEXT NOT NULL,
                    data_sources TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS recipes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    ingredients TEXT,
                    dish_name TEXT,
                    url TEXT NOT NULL,
                    data_sources TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ingredients ON recipes(ingredients)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_video_id ON recipes(video_id)")
        
        conn.commit()
        conn.close()
        logger.info("데이터베이스 초기화 완료")
    except Exception as e:
        logger.error(f"데이터베이스 초기화 실패: {e}")

init_database()

def check_if_video_exists(video_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM recipes WHERE video_id = ?", (video_id,))
    exists = cursor.fetchone()[0] > 0
    conn.close()
    return exists

# --- YouTube 데이터 수집 ---
def get_playlist_items(playlist_id):
    video_ids = []
    next_page_token = None
    
    try:
        while True:
            request = youtube.playlistItems().list(
                part="contentDetails",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=next_page_token
            )
            response = request.execute()
            
            for item in response["items"]:
                if 'contentDetails' in item and 'videoId' in item['contentDetails']:
                    video_ids.append(item["contentDetails"]["videoId"])
            
            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break
                
        logger.info(f"플레이리스트에서 {len(video_ids)}개의 영상 발견")
        return video_ids
    except Exception as e:
        logger.error(f"플레이리스트 가져오기 실패: {e}")
        return []

def get_video_info(video_id):
    try:
        request = youtube.videos().list(part="snippet", id=video_id)
        response = request.execute()
        
        if not response["items"]:
            return None
        
        video = response["items"][0]
        return {
            'title': video["snippet"]["title"],
            'description': video["snippet"]["description"],
            'url': f"https://www.youtube.com/watch?v={video_id}"
        }
    except Exception as e:
        logger.error(f"비디오 정보 가져오기 실패: {e}")
        return None

def get_video_transcript(video_id):
    """자막 가져오기 - 최종 수정 버전"""
    
    LANGUAGES_TO_CHECK = ['ko', 'en']
    
    try:
        # 1. YouTubeTranscriptApi 인스턴스 생성
        ytt_api = YouTubeTranscriptApi() 
        
        # 2. 사용 가능한 자막 트랙 목록 가져오기
        transcript_list = ytt_api.list(video_id)
        
        transcript = None
        
        # 3. 수동 생성 자막 시도 (우선)
        try:
            transcript = transcript_list.find_manually_created_transcript(LANGUAGES_TO_CHECK)
            logger.info(f"수동 자막 발견: {transcript.language_code}")
        except NoTranscriptFound:
            # 4. 자동 생성 자막 시도
            try:
                transcript = transcript_list.find_generated_transcript(LANGUAGES_TO_CHECK)
                logger.info(f"자동 자막 발견: {transcript.language_code}")
            except NoTranscriptFound:
                logger.warning(f"자막 없음: {video_id}에 대해 {LANGUAGES_TO_CHECK} 자막을 찾을 수 없습니다.")
                return None
        
        # 5. 자막 내용 추출
        transcript_data = transcript.fetch()
        print(f"✅ 자막 데이터 타입: {type(transcript_data)}")
        if transcript_data:
            print(f"✅ 첫 번째 항목 타입: {type(transcript_data[0])}")
            print(f"✅ 첫 번째 항목 내용: {transcript_data[0]}")
            print(f"✅ 첫 번째 항목 속성: {dir(transcript_data[0])}")
                
        # 🔥 핵심 수정: 속성 접근 방식으로 변경
        # FetchedTranscriptSnippet 객체는 .text 속성을 가지고 있음
        text_parts = []
        for snippet in transcript_data:
            # 속성 접근 방식 사용
            if hasattr(snippet, 'text'):
                text_parts.append(snippet.text)
            # 혹시 딕셔너리인 경우도 대비
            elif isinstance(snippet, dict):
                text_parts.append(snippet.get('text', ''))
        
        text = ' '.join(text_parts)
        
        if text:
            logger.info(f"자막 추출 성공 ({len(text)}자)")
            return text
        else:
            logger.warning(f"자막 데이터가 비어있음: {video_id}")
            return None
        
    except TranscriptsDisabled:
        logger.warning(f"자막 비활성화: {video_id}")
        return None
    except VideoUnavailable:
        logger.error(f"비디오 사용 불가: {video_id}")
        return None
    except NoTranscriptFound:
        logger.warning(f"자막 없음: {video_id}")
        return None
    except Exception as e:
        logger.error(f"자막 가져오기 실패 (최종 오류): {type(e).__name__} - {e}")
        # 상세 디버깅 정보
        import traceback
        logger.debug(f"상세 에러:\n{traceback.format_exc()}")
        return None

def get_video_comments(video_id, max_comments=8):
    """댓글 가져오기 (상위 8개)"""
    try:
        request = youtube.commentThreads().list(
            part="snippet",
            videoId=video_id,
            maxResults=max_comments,
            order="relevance"  # 관련성 높은 순
        )
        response = request.execute()
        
        comments = []
        for item in response.get("items", []):
            comment = item["snippet"]["topLevelComment"]["snippet"]["textDisplay"]
            # HTML 태그 제거
            comment = re.sub(r'<[^>]+>', '', comment)
            comments.append(comment)
        
        comments_text = ' | '.join(comments)
        logger.info(f"댓글 {len(comments)}개 가져옴")
        return comments_text
    except Exception as e:
        logger.warning(f"댓글 가져오기 실패: {e}")
        return None

# --- Gemini 분석 ---
def analyze_with_gemini(data_dict, title):
    """모든 수집 데이터를 Gemini로 종합 분석"""
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        # 데이터 조합
        available_data = []
        if data_dict.get('transcript'):
            available_data.append(f"자막: {data_dict['transcript'][:2000]}")
        if data_dict.get('description'):
            available_data.append(f"설명: {data_dict['description'][:1000]}")
        if data_dict.get('comments'):
            available_data.append(f"댓글: {data_dict['comments'][:800]}")
        
        if not available_data:
            return title, "", []
        
        combined_text = "\n\n".join(available_data)
        
        prompt = f"""다음은 "{title}"라는 요리 영상의 데이터입니다.
이 데이터를 종합 분석하여 요리 이름과 재료를 추출하세요.

{combined_text}

규칙:
1. 요리 이름은 간단명료하게
2. 재료는 쉼표로만 구분, 공백 없이
3. 기본 조미료, 양념 포함
4. 댓글에서 언급된 재료도 고려
5. JSON 형식으로 응답

응답 형식:
{{"dish_name": "요리이름", "ingredients": "재료1,재료2,재료3"}}
"""
        
        response = model.generate_content(prompt)
        result = response.text.strip()
        
        # JSON 추출
        result = re.sub(r'^```json?\s*', '', result)
        result = re.sub(r'\s*```$', '', result)
        
        # 🚨 핵심 수정 부분 시작
        data = json.loads(result)
        
        # 💡 수정 1: 응답이 리스트인 경우 첫 번째 항목을 데이터로 사용
        # 'list' object has no attribute 'get' 오류 해결
        if isinstance(data, list):
            if data:
                data = data[0] # 리스트의 첫 번째 항목(딕셔너리)을 사용
            else:
                # 빈 리스트일 경우
                logger.error("Gemini가 빈 리스트를 반환했습니다.")
                return title, "", []

        # 이제 data는 딕셔너리이므로 .get()을 안전하게 사용할 수 있습니다.
        dish_name = data.get('dish_name', title)
        ingredients = data.get('ingredients', '')
        # 🚨 핵심 수정 부분 끝
        
        if isinstance(ingredients, list):
            ingredients = ','.join(ingredients)
        
        # 정리
        ingredients = re.sub(r'\s+', '', ingredients)
        ingredients = re.sub(r',+', ',', ingredients)
        ingredients = ingredients.strip(',')
        
        # 사용된 데이터 소스 기록
        sources = []
        if data_dict.get('transcript'):
            sources.append('자막')
        if data_dict.get('description'):
            sources.append('설명')
        if data_dict.get('comments'):
            sources.append('댓글')
        
        logger.info(f"Gemini 분석 완료: {dish_name}, 소스: {sources}")
        return dish_name, ingredients, sources
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON 파싱 실패: {e}")
        return title, "", []
    except Exception as e:
        logger.error(f"Gemini 분석 실패: {e}")
        return title, "", []

# --- 진행 상황 ---
def update_status(session_id, current, total, status_text, video_title=""):
    with status_lock:
        processing_status[session_id] = {
            'current': current,
            'total': total,
            'percentage': int((current / total) * 100) if total > 0 else 0,
            'status': status_text,
            'video_title': video_title,
            'timestamp': time.time()
        }

# --- 메인 처리 ---
def process_single_video(video_id, session_id, current_index, total_videos):
    """단일 비디오 처리 (YouTube API만 사용)"""
    
    if check_if_video_exists(video_id):
        logger.info(f"[{video_id}] 이미 처리됨")
        update_status(session_id, current_index, total_videos, "이미 처리된 영상")
        return {"status": "skipped", "video_id": video_id}
    
    try:
        # 1. 비디오 정보
        update_status(session_id, current_index, total_videos, "영상 정보 가져오는 중...")
        video_info = get_video_info(video_id)
        if not video_info:
            return {"status": "error", "video_id": video_id}
        
        title = video_info['title']
        description = video_info['description']
        video_url = video_info['url']
        
        logger.info(f"처리 시작: {title}")
        
        # 2. 데이터 수집
        data_dict = {}
        
        # 2-1. 자막
        update_status(session_id, current_index, total_videos, "자막 확인 중...", title)
        transcript = get_video_transcript(video_id)
        if transcript:
            data_dict['transcript'] = transcript
        
        # 2-2. 설명
        if description:
            data_dict['description'] = description
        
        # 2-3. 댓글
        update_status(session_id, current_index, total_videos, "댓글 수집 중...", title)
        comments = get_video_comments(video_id)
        if comments:
            data_dict['comments'] = comments
        
        # 3. Gemini 분석
        update_status(session_id, current_index, total_videos, "AI 분석 중...", title)
        dish_name, ingredients, sources = analyze_with_gemini(data_dict, title)
        
        # 4. DB 저장
        if not ingredients:
            logger.warning(f"재료 추출 실패: {title}")
            ingredients = ""
        
        sources_str = ','.join(sources) if sources else "없음"
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO recipes (video_id, title, description, ingredients, dish_name, url, data_sources)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (video_id, title, description, ingredients, dish_name, video_url, sources_str))
        conn.commit()
        conn.close()
        
        update_status(session_id, current_index, total_videos, "완료!", title)
        logger.info(f"저장 완료: {title} | 소스: {sources_str} | 재료: {ingredients[:50] if ingredients else '없음'}")
        
        return {
            "status": "success",
            "video_id": video_id,
            "title": title,
            "dish_name": dish_name,
            "sources": sources
        }
        
    except Exception as e:
        logger.error(f"비디오 처리 실패 ({video_id}): {e}")
        update_status(session_id, current_index, total_videos, f"오류: {str(e)[:30]}")
        return {"status": "error", "video_id": video_id, "message": str(e)}

# --- Flask 라우트 ---
@app.route('/')
def index():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM recipes")
        count = cursor.fetchone()[0]
        conn.close()
    except:
        count = 0
    
    return f'''
        <!DOCTYPE html>
        <html lang="ko">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>레시피 추출 시스템 v4</title>
            <style>
                body {{
                    font-family: 'Segoe UI', sans-serif;
                    max-width: 800px;
                    margin: 50px auto;
                    padding: 20px;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                }}
                .container {{
                    background: white;
                    padding: 40px;
                    border-radius: 20px;
                    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                }}
                h1 {{
                    color: #333;
                    text-align: center;
                    margin-bottom: 10px;
                }}
                .subtitle {{
                    text-align: center;
                    color: #666;
                    margin-bottom: 30px;
                    font-size: 14px;
                }}
                .badge {{
                    background: linear-gradient(135deg, #667eea, #764ba2);
                    color: white;
                    padding: 4px 12px;
                    border-radius: 12px;
                    font-size: 12px;
                    font-weight: bold;
                }}
                .features {{
                    background: #f8f9fa;
                    padding: 20px;
                    border-radius: 10px;
                    margin: 20px 0;
                }}
                .feature-item {{
                    margin: 10px 0;
                }}
                .stats {{
                    background: #e3f2fd;
                    padding: 20px;
                    border-radius: 10px;
                    margin: 20px 0;
                    text-align: center;
                }}
                .stats-number {{
                    font-size: 36px;
                    font-weight: bold;
                    color: #667eea;
                }}
                input[type="text"] {{
                    width: 100%;
                    padding: 15px;
                    margin: 10px 0;
                    border: 2px solid #ddd;
                    border-radius: 10px;
                    box-sizing: border-box;
                    font-size: 16px;
                }}
                button {{
                    width: 100%;
                    padding: 15px;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    border: none;
                    border-radius: 10px;
                    cursor: pointer;
                    font-size: 18px;
                    font-weight: bold;
                }}
                .link {{
                    display: block;
                    text-align: center;
                    margin-top: 20px;
                    color: #667eea;
                    text-decoration: none;
                    font-weight: bold;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🍳 레시피 추출 시스템</h1>
                <p class="subtitle">
                    <span class="badge">v4</span> 안전하고 합법적인 방식 - YouTube API 공식 사용
                </p>
                
                <div class="features">
                    <strong>📊 데이터 소스 (3가지):</strong>
                    <div class="feature-item">✅ 자막 (한국어/영어)</div>
                    <div class="feature-item">✅ 영상 설명</div>
                    <div class="feature-item">✅ 댓글 (상위 8개)</div>
                </div>
                
                <div class="stats">
                    <div class="stats-number">{count}</div>
                    <div>개의 레시피 저장됨</div>
                </div>
                
                <form method="post" action="/process">
                    <label for="playlist_url"><strong>플레이리스트 URL:</strong></label>
                    <input type="text" id="playlist_url" name="playlist_url" 
                           placeholder="https://www.youtube.com/playlist?list=..." required>
                    <button type="submit">🚀 분석 시작 (최대 10개)</button>
                </form>
                
                <a href="/recommend" class="link">📋 레시피 추천받기 →</a>
            </div>
        </body>
        </html>
    '''

@app.route('/process', methods=['POST'])
def process_playlist():
    playlist_url = request.form.get('playlist_url')
    
    if not playlist_url:
        return "플레이리스트 URL을 입력하세요.", 400
    
    match = re.search(r'list=([a-zA-Z0-9_-]+)', playlist_url)
    if not match:
        return "유효하지 않은 플레이리스트 URL입니다.", 400
    
    playlist_id = match.group(1)
    session_id = os.urandom(16).hex()
    session['processing_id'] = session_id
    
    return redirect(url_for('process_playlist_manual', playlist_id=playlist_id, session_id=session_id))

@app.route('/process_playlist/<playlist_id>')
def process_playlist_manual(playlist_id):
    session_id = request.args.get('session_id', os.urandom(16).hex())
    session['processing_id'] = session_id
    
    video_ids = get_playlist_items(playlist_id)
    
    if not video_ids:
        return "플레이리스트를 불러올 수 없습니다.", 400
    
    original_count = len(video_ids)
    if len(video_ids) > FREE_TIER_LIMIT:
        video_ids = video_ids[:FREE_TIER_LIMIT]
        limited = True
    else:
        limited = False
    
    return render_template('processing.html', 
                         session_id=session_id, 
                         total_videos=len(video_ids),
                         original_count=original_count,
                         limited=limited,
                         playlist_id=playlist_id)

@app.route('/start_processing/<playlist_id>/<session_id>')
def start_processing(playlist_id, session_id):
    video_ids = get_playlist_items(playlist_id)
    
    if len(video_ids) > FREE_TIER_LIMIT:
        video_ids = video_ids[:FREE_TIER_LIMIT]
    
    update_status(session_id, 0, len(video_ids), "처리 준비 중...")
    
    def process_videos():
        results = []
        for idx, video_id in enumerate(video_ids, 1):
            result = process_single_video(video_id, session_id, idx, len(video_ids))
            results.append(result)
            time.sleep(2)
        
        success_count = sum(1 for r in results if r.get('status') == 'success')
        with status_lock:
            processing_status[session_id]['completed'] = True
            processing_status[session_id]['success_count'] = success_count
            processing_status[session_id]['total'] = len(video_ids)
    
    import threading
    thread = threading.Thread(target=process_videos)
    thread.daemon = True
    thread.start()
    
    return jsonify({"status": "started"})

@app.route('/status/<session_id>')
def get_status(session_id):
    with status_lock:
        status = processing_status.get(session_id, {
            'current': 0,
            'total': 0,
            'percentage': 0,
            'status': '준비 중...',
            'video_title': '',
            'completed': False
        })
    return jsonify(status)

@app.route('/recommend')
def recommend_page():
    return render_template('recommend.html')

@app.route('/recommend', methods=['POST'])
def recommend_recipe():
    user_ingredients_input = request.form.get('ingredients', '')
    
    if not user_ingredients_input:
        return render_template('recommend.html', message="재료를 입력해주세요.")
    
    user_ingredients = set(i.strip() for i in user_ingredients_input.split(',') if i.strip())
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    conditions = " OR ".join(["ingredients LIKE ?" for _ in user_ingredients])
    values = [f"%{ing}%" for ing in user_ingredients]
    
    query = f"SELECT * FROM recipes WHERE {conditions}"
    cursor.execute(query, values)
    results = cursor.fetchall()
    conn.close()
    
    if not results:
        return render_template('recommend.html', 
                             message="해당 재료로 만들 수 있는 레시피를 찾을 수 없습니다.")
    
    recipes = []
    for row in results:
        recipe_ings = set(i.strip() for i in row['ingredients'].split(',') if i.strip())
        matched = user_ingredients & recipe_ings
        missing = recipe_ings - user_ingredients
        
        match_rate = (len(matched) / len(recipe_ings) * 100) if recipe_ings else 0
        
        recipes.append({
            'title': row['title'],
            'url': row['url'],
            'dish_name': row['dish_name'],
            'match_rate': f"{match_rate:.1f}",
            'matched': ', '.join(matched),
            'missing': ', '.join(missing),
            'all_ingredients': ', '.join(recipe_ings),
            'sources': row['data_sources'] if row['data_sources'] is not None else '없음'
        })
    
    recipes.sort(key=lambda x: float(x['match_rate']), reverse=True)
    
    return render_template('recommend.html', 
                         recipes=recipes, 
                         user_ingredients=user_ingredients_input)

if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("DEBUG", "True").lower() == "true"
    app.run(host='0.0.0.0', port=port, debug=debug)