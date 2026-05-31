import os
import re
import html
import time
import json
import logging
import socket
from datetime import datetime
import urllib.request
import xml.etree.ElementTree as ET
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# 無限フリーズを根底から克服するグローバルタイムアウト設定
socket.setdefaulttimeout(30)

# ==========================================
# 1. ログ・フォルダ初期設定
# ==========================================
os.makedirs("logs", exist_ok=True)
os.makedirs("articles", exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs("books", exist_ok=True)

logging.basicConfig(
    filename="logs/app.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

MAX_ARTICLES_LIMIT = 30
MAX_HISTORY_LIMIT = 5000
TEMPLATE_VERSION = "4.0.0"  # 3号店 v4.0仕様

# ==========================================
# 2. Pydanticスキーマ定義（三層構造 ＆ 検索意図対応）
# ==========================================
class ArticleOutputSchema(BaseModel):
    title: str = Field(description="不安（損したくない）×欲望（得したい）×優越（リードしたい）を刺激する、35文字以内の強力なバズタイトル。指定されたSEOキーワードを自然に含めること。")
    search_intent: str = Field(description="読者の検索意図を自動判定し、'KNOW'（知りたい）、'DO'（行動したい）、'GO'（特定の場所・状態へ向かいたい）のいずれか半角大文字1語で出力。")
    instant_answer: str = Field(description="【3秒エリア】読者の疑問に即座に100点満点の結論を出す一文。体言止めで45文字以内。")
    summary_detail: str = Field(description="【30秒エリア】なぜそうなるのかの論理的・構造的背景の解説。元記事の具体的なデータや一節の日本語訳を適切に引用しながら、500〜700文字程度で論理的に詳細記述。")
    charo_insight: str = Field(description="【2分エリア：編集長cocoroの眼】激変期にどうやって人生の操縦席を守り賢く生き抜くかの人生・実務戦略インサイト。投資・キャリア・心理面を統合したプロの視点（200〜300文字程度）。")
    today_mission: str = Field(description="【2分エリア：明日からの具体的ミッション】読者が『まず今すぐ確認・行動すべき実用的なアクション』を箇条書きではなく1つの力強い文章で記述。100文字程度。")
    slug: str = Field(description="ファイル名に使用する半角英数字とハイフンのみのスラグ。例: 'ai-career-survival-strategy'")

# ==========================================
# 3. 各種ユーティリティ関数
# ==========================================
def sanitize_slug(raw_slug: str) -> str:
    slug = re.sub(r'[^a-z0-9\-]', '', raw_slug.lower())
    slug = re.sub(r'-+', '-', slug).strip('-')
    if not slug:
        slug = f"life-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    return slug[:80]

def get_strategy_context(article_text: str) -> str:
    strategy_path = os.path.join("data", "strategy_master.json")
    if not os.path.exists(strategy_path):
        return ""
    try:
        with open(strategy_path, "r", encoding="utf-8") as f:
            strategy_data = json.load(f)
        
        matched_info = []
        text_lower = article_text.lower()
        
        for key, value in strategy_data.items():
            trigger = value.get("keyword_trigger", "").lower()
            if trigger and (trigger in text_lower or key.lower() in text_lower):
                keywords_str = ", ".join(value.get("seo_keywords", []))
                links_str = "\n".join([f"- [{l['title']}]({l['url']})" for l in value.get("trust_links", [])])
                matched_info.append(f"【戦略カテゴリ: {key}】\n■必ず盛り込むSEOキーワード: {keywords_str}\n■引用すべき高信頼外部リンク:\n{links_str}")
        
        if matched_info:
            logging.info(f"戦略マスターデータ突合成功: {len(matched_info)}")
            return "\n\n=== 突合された人生・実務戦略マスター情報 ===\n" + "\n\n".join(matched_info)
    except Exception as e:
        logging.error(f"戦略マスターデータ突合失敗: {e}")
    return ""

# ==========================================
# 4. 履歴管理
# ==========================================
HISTORY_FILE = "logs/history.json"

def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            return [item for item in raw_data if isinstance(item, dict) and "url" in item]
        except Exception as e:
            logging.error(f"履歴ファイル読み込み失敗: {e}")
    return []

def save_history(history: list):
    try:
        trimmed_history = history[-MAX_HISTORY_LIMIT:]
        tmp_file = HISTORY_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(trimmed_history, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, HISTORY_FILE)
    except Exception as e:
        logging.error(f"履歴ファイル保存失敗: {e}")

# ==========================================
# 5. RSS取得・スクレイピング
# ==========================================
def fetch_rss_feed(rss_url: str) -> list:
    articles = []
    try:
        logging.info(f"RSSを取得中: {rss_url}")
        req = urllib.request.Request(
            rss_url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            root = ET.fromstring(response.read())
        for item in root.findall('.//item'):
            title = item.find('title').text if item.find('title') is not None else ""
            link = item.find('link').text if item.find('link') is not None else ""
            description = item.find('description').text if item.find('description') is not None else ""
            articles.append({"title": title, "link": link, "description": description})
    except Exception as e:
        logging.error(f"RSS取得パース失敗 ({rss_url}): {e}")
    return articles

def fetch_full_article_text(url: str) -> str:
    try:
        logging.info(f"元記事全文を取得中: {url}")
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            html_content = response.read().decode('utf-8', errors='ignore')
        
        for tag in ['script', 'style', 'header', 'footer', 'nav']:
            html_content = re.sub(f'<{tag}[\\s\\S]*?>[\\s\\S]*?</{tag}>', '', html_content)
        html_content = re.sub(r'</?(p|div|h1|h2|h3|h4|li|br)[^>]*>', '\n', html_content)
        text = re.sub(r'<[^>]+>', ' ', html_content)
        text = html.unescape(text)
        return re.sub(r'\n\s*\n+', '\n', text).strip()
    except Exception as e:
        logging.warning(f"元記事全文取得失敗（RSS抜粋へフォールバックします）: {e}")
        return ""

# ==========================================
# 6. コア：AI記事生成
# ==========================================
def run_article_generator(source_text: str, source_url: str, source_name: str) -> str:
    # 改善②：重い処理の直前でAPIキーの有無を最優先チェック（無駄なディスクI/O防止ガード）
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logging.error("GEMINI_API_KEY が設定されていません。")
        return ""

    safe_source_text = source_text[:12000]
    strategy_context = get_strategy_context(safe_source_text)

    client = genai.Client(api_key=api_key)
    # 行動憲章に基づき「gemini-2.5-flash」を完全固定
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    # 改善④：プロンプト内の読点ブレを完璧に修復した、3号店専用の究極システムプロンプト
    prompt = f"""
    あなたは、激変するAI時代において「人間がいかにして人生の操縦席を守り、賢く生き抜くか」という【人生の意思決定OS・判断基準】を授ける最高峰のライフ戦略編集長です。
    提供された【海外ニュース】と【人生・実務戦略マスター情報】を厳密にマージし、以下の【ルール】に沿って全自動執筆してください。

    【ルール】
    - プロンプトのハック（How）ではなく、時代の変化の本質（Why）に焦点を当ててください。
    - タイトルは、人間の感情の3点セット「不安（損したくない）」「欲望（得したい）」「優越（リードしたい）」を絶妙に刺激し、かつ突合されたSEOキーワードを必ず1つ以上自然に含めて35文字以内で作成してください。
    - search_intentは、読者が何を求めているかを分析し、KNOW、DO、GOのいずれか1語で判定してください。
    - instant_answer（3秒エリア）は、読者の疑問を瞬時に解決する極上の1文を体言止め45文字以内で記述してください。
    - summary_detail（30秒エリア）は、ニュースの具体的なデータや数値を適切に「引用」しながら、その背景にある構造的変化を500〜700文字程度で深く解説してください。
    - charo_insight（2分エリア：編集長cocoroの眼）は、読者に「なるほど！」と膝を打たせる、明日から迷わずに生きるための人生・実務戦略のインサイト（200〜300文字）を記述してください。
    - today_missionは、読者が今日から、または明日から今すぐ行動に移せる具体的かつ実践的なミッションを1文（100文字程度）で力強く提示してください。
    - slugは半角英数字とハイフンのみ。

    【海外ニュース】
    {safe_source_text}
    {strategy_context}
    """

    # 改善③：一時的なAPI瞬断や429レート制限を自動でいなす指数バックオフ付きリトライ機構
    MAX_RETRIES = 3
    response_text = ""

    for attempt in range(MAX_RETRIES):
        try:
            logging.info(f"Gemini API呼び出し中 (試行 {attempt + 1}/{MAX_RETRIES})...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ArticleOutputSchema,
                    http_options=types.HttpOptions(timeout=60000)
                )
            )
            if response and response.text:
                response_text = response.text.strip()
                break
            else:
                raise ValueError("APIレスポンスのテキストが空でした。")
        except Exception as e:
            wait = 2 ** attempt
            logging.warning(f"API接続一時失敗（試行 {attempt + 1}）: {e}。{wait}秒待機してリトライします...")
            time.sleep(wait)
    else:
        logging.error("リトライ制限超過のため生成を中止します。")
        return ""

    response_text = re.sub(r"^```json\s*|\s*```$", "", response_text, flags=re.IGNORECASE).strip()

    try:
        data = json.loads(response_text)
        validated_data = ArticleOutputSchema(**data)
    except Exception as e:
        logging.error(f"Pydanticバリデーション失敗: {e}\n出力: {response_text}")
        return ""

    art = validated_data.model_dump()
    slug = sanitize_slug(art["slug"])

    # 改善①：無言失敗を完全に防ぐ bool 戻り値チェックによるレイアウト結合
    success = build_page(
        body_template_path="template_article.html",
        title=art["title"],
        date_iso=datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        date_ja=datetime.now().strftime("%Y年%m月%d日 %H:%M"),
        source_url=source_url,
        source_name=source_name,
        replacements={
            "{{INSTANT_ANSWER}}": art["instant_answer"],
            "{{SUMMARY_DETAIL}}": art["summary_detail"],
            "{{CHARO_INSIGHT}}": art["charo_insight"],
            "{{TODAY_MISSION}}": art["today_mission"],
            "{{SEARCH_INTENT}}": art["search_intent"]
        },
        output_path=os.path.join("articles", f"{slug}.html"),
        is_article=True,
        slug=slug
    )
    
    if not success:
        logging.error(f"記事HTMLの結合生成に失敗しました: {slug}")
        return ""

    # マスターJSONデータの保存
    art["source_url"] = source_url
    art["source_name"] = source_name
    art["template_version"] = TEMPLATE_VERSION
    output_json_path = os.path.join("data", f"{slug}.json")
    
    try:
        tmp_json = output_json_path + ".tmp"
        with open(tmp_json, "w", encoding="utf-8") as f:
            json.dump(art, f, ensure_ascii=False, indent=2)
        os.replace(tmp_json, output_json_path)
        logging.info(f"記事生成・JSON保存成功: {slug}")
        return slug
    except Exception as e:
        logging.error(f"JSON保存失敗: {e}")
        return ""

# ==========================================
# 7. レイアウト結合ヘルパー（json.dumpsによる最強のJSON-LDシリアライズ完備）
# ==========================================
def build_page(body_template_path, title, date_iso, date_ja, source_url, source_name, replacements, output_path, is_article=False, slug="", raw_html_keys: list = None) -> bool:
    try:
        if not os.path.exists("layout.html") or not os.path.exists(body_template_path):
            logging.error(f"必要なテンプレートファイルが見つかりません。")
            return False

        with open("layout.html", "r", encoding="utf-8") as f:
            layout_content = f.read()
        with open(body_template_path, "r", encoding="utf-8") as f:
            body_content = f.read()

        combined_content = layout_content.replace("{{BODY_CONTENT}}", body_content)

        if raw_html_keys is None:
            raw_html_keys = ["{{ARTICLES_GRID}}", "{{WEEKLY_BOOK_BANNER}}", "{{BOOK_CONTENT}}"]

        # 確実な安全エスケープ置換ループ
        for placeholder, value in replacements.items():
            if placeholder in raw_html_keys:
                combined_content = combined_content.replace(placeholder, value)
            else:
                combined_content = combined_content.replace(placeholder, html.escape(value))

        # 構造化データの json.dumps による100%構文崩れ防止シリアライズ
        if is_article:
            combined_content = combined_content.replace("{{CSS_PATH}}", "/style.css")
            combined_content = combined_content.replace("{{JS_PATH}}", "/script.js")
            
            ld_json_data = {
                "@context": "https://schema.org",
                "@type": "Article",
                "headline": title,
                "datePublished": date_iso,
                "author": {
                    "@type": "Person",
                    "name": "cocoro"
                },
                "description": replacements.get("{{INSTANT_ANSWER}}", title),
                "mainEntityOfPage": source_url
            }
            serialized_json = json.dumps(ld_json_data, ensure_ascii=False, indent=2)
            structured_data = f'<script type="application/ld+json">\n{serialized_json}\n</script>'
            combined_content = combined_content.replace("{{STRUCTURED_DATA}}", structured_data)
        else:
            combined_content = combined_content.replace("{{CSS_PATH}}", "style.css")
            combined_content = combined_content.replace("{{JS_PATH}}", "script.js")
            structured_data = """
            <script type="application/ld+json">
            {
              "@context": "https://schema.org",
              "@type": "WebSite",
              "name": "AI Frontier Life",
              "url": "https://ai-life.pray-power-is-god-and-cocoro.com/"
            }
            </script>
            """
            combined_content = combined_content.replace("{{STRUCTURED_DATA}}", structured_data)

        # 共通平文要素の確実な置換
        combined_content = combined_content.replace("{{TITLE}}", html.escape(title))
        combined_content = combined_content.replace("{{DATE_ISO}}", date_iso)
        combined_content = combined_content.replace("{{DATE_JA}}", date_ja)
        combined_content = combined_content.replace("{{SOURCE_URL}}", html.escape(source_url))
        combined_content = combined_content.replace("{{SOURCE_NAME}}", html.escape(source_name))

        tmp_output = output_path + ".tmp"
        with open(tmp_output, "w", encoding="utf-8") as f:
            f.write(combined_content)
        os.replace(tmp_output, output_path)
        return True
    except Exception as e:
        logging.error(f"build_page 実行エラー ({output_path}): {e}")
        return False

# ==========================================
# 8. サイト内完結型プチ電子書籍バナー生成
# ==========================================
def get_weekly_book_banner_html() -> str:
    if not os.path.exists("books"):
        return ""
    book_files = [f for f in os.listdir("books") if f.endswith(".html")]
    if not book_files:
        return ""
    
    book_files.sort(key=lambda x: os.path.getmtime(os.path.join("books", x)), reverse=True)
    latest_book_file = book_files[0]
    book_slug = os.path.splitext(latest_book_file)[0]
    
    display_title = f"{datetime.now().strftime('%Y年%m月')} 最新号：AI時代の人生戦略・究極バイブル"
    
    return f"""
    <section class="weekly-book-banner fade-element" style="margin-bottom: 40px;">
        <div style="background: linear-gradient(135deg, #1e293b, #334155); color: white; padding: 30px; border-radius: 16px; box-shadow: 0 8px 24px rgba(0,0,0,0.08); text-align: center;">
            <span style="background: rgba(255, 255, 255, 0.15); padding: 4px 12px; border-radius: 999px; font-size: 0.8rem; font-weight: 800; letter-spacing: 0.05em;">🆕 AI WEEKLY BOOK 配信中</span>
            <h2 style="font-size: 1.6rem; font-weight: 800; margin: 15px 0 10px; color: white;">{display_title}</h2>
            <p style="font-size: 0.95rem; color: rgba(255, 255, 255, 0.85); max-width: 500px; margin: 0 auto 20px; line-height: 1.6;">今週の変化を体系的に統合。これからの激変期に、自分自身のマインドとキャリアの主導権を完全に守り抜くための特別統合レポートです。</p>
            <a href="books/{book_slug}.html" class="toggle-button" style="background: white; color: #1e293b; border: none; font-weight: 800; margin-top: 0; display: inline-block; padding: 12px 24px; border-radius: 8px; text-decoration: none;">電子書籍を読む（無料） &rarr;</a>
        </div>
    </section>
    """

# ==========================================
# 9. 再ビルド（SSGコンパイル ＆ ローテーション）
# ==========================================
def rebuild_index_and_rotate_storage():
    try:
        json_files = [f for f in os.listdir("data") if f.endswith(".json") and f != "strategy_master.json"]
        all_articles = []

        for j_file in json_files:
            path = os.path.join("data", j_file)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    article_data = json.load(f)
                mtime = os.path.getmtime(path)
                all_articles.append((mtime, article_data))
            except Exception as e:
                logging.error(f"JSON読み込み失敗 ({j_file}): {e}")

        all_articles.sort(key=lambda x: x[0], reverse=True)

        # 古い記事のローテーション自動削除
        if len(all_articles) > MAX_ARTICLES_LIMIT:
            logging.info("上限超過のため古いデータをローテーション削除します。")
            to_delete = all_articles[MAX_ARTICLES_LIMIT:]
            all_articles = all_articles[:MAX_ARTICLES_LIMIT]
            for _, d_art in to_delete:
                d_slug = sanitize_slug(d_art["slug"])
                for p in [os.path.join("articles", f"{d_slug}.html"), os.path.join("data", f"{d_slug}.json")]:
                    if os.path.exists(p):
                        os.remove(p)

        if not all_articles:
            logging.info("データが空のため、一覧の更新を保留します。")
            return

        # すべての個別記事を再コンパイル
        for mtime, art in all_articles:
            a_slug = sanitize_slug(art["slug"])
            a_date_ja = datetime.fromtimestamp(mtime).strftime("%Y年%m月%d日 %H:%M")
            a_date_iso = datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S+09:00")
            
            build_page(
                body_template_path="template_article.html",
                title=art["title"],
                date_iso=a_date_iso,
                date_ja=a_date_ja,
                source_url=art.get("source_url", "#"),
                source_name=art.get("source_name", "ソース"),
                replacements={
                    "{{INSTANT_ANSWER}}": art["instant_answer"],
                    "{{SUMMARY_DETAIL}}": art["summary_detail"],
                    "{{CHARO_INSIGHT}}": art.get("charo_insight", "最新インサイトです。"),
                    "{{TODAY_MISSION}}": art["today_mission"],
                    "{{SEARCH_INTENT}}": art.get("search_intent", "KNOW")
                },
                output_path=os.path.join("articles", f"{a_slug}.html"),
                is_article=True,
                slug=a_slug
            )

        _, hero_art = all_articles[0]
        hero_date_ja = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        hero_date_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

        # グリッド部分のHTML生成
        articles_html = ""
        for _, art in all_articles[1:]:
            safe_title = html.escape(art["title"])
            safe_answer = html.escape(art["instant_answer"])
            safe_slug = sanitize_slug(art["slug"])
            intent = html.escape(art.get("search_intent", "KNOW"))
            
            articles_html += f"""
                <article class="article-card fade-element">
                    <div class="article-meta">
                        <span class="intent-badge">{intent}</span>
                        <span>Latest Release</span>
                    </div>
                    <h3>{safe_title}</h3>
                    <p>{safe_answer}</p>
                    <a href="articles/{safe_slug}.html">戦略を読む &rarr;</a>
                </article>
            """

        weekly_book_banner = get_weekly_book_banner_html()

        # index.htmlのビルド
        build_page(
            body_template_path="template_index.html",
            title=hero_art["title"],
            date_iso=hero_date_iso,
            date_ja=hero_date_ja,
            source_url=hero_art.get("source_url", "#"),
            source_name=hero_art.get("source_name", "ソース"),
            replacements={
                "{{INSTANT_ANSWER}}": hero_art["instant_answer"],
                "{{SUMMARY_DETAIL}}": hero_art["summary_detail"],
                "{{CHARO_INSIGHT}}": hero_art.get("charo_insight", "最新インサイトです。"),
                "{{TODAY_MISSION}}": hero_art["today_mission"],
                "{{SEARCH_INTENT}}": hero_art.get("search_intent", "KNOW"),
                "{{ARTICLES_GRID}}": articles_html,
                "{{WEEKLY_BOOK_BANNER}}": weekly_book_banner
            },
            output_path="index.html",
            is_article=False
        )

        # archive.htmlのビルド
        archive_articles_html = ""
        for _, art in all_articles:
            a_title = html.escape(art["title"])
            a_answer = html.escape(art["instant_answer"])
            a_slug = sanitize_slug(art["slug"])
            intent = html.escape(art.get("search_intent", "KNOW"))
            archive_articles_html += f"""
                <article class="article-card fade-element">
                    <div class="article-meta">
                        <span class="intent-badge">{intent}</span>
                        <span>Archived</span>
                    </div>
                    <h3>{a_title}</h3>
                    <p>{a_answer}</p>
                    <a href="articles/{a_slug}.html">戦略を読む &rarr;</a>
                </article>
            """

        success_archive = build_page(
            body_template_path="template_archive.html",
            title="過去の人生・実務戦略アーカイブ",
            date_iso=hero_date_iso,
            date_ja=hero_date_ja,
            source_url="#",
            source_name="アーカイブ",
            replacements={"{{ARTICLES_GRID}}": archive_articles_html},
            output_path="archive.html",
            is_article=False
        )
        if not success_archive:
            logging.critical("【重大エラー】template_archive.html に起因して archive.html の生成に失敗しました。")

        print("✅ 3号店：インデックス、アーカイブ、全記事の再ビルドが完了しました！")
    except Exception as e:
        logging.error(f"再ビルド中に重大なエラーが発生しました: {e}")

# ==========================================
# 10. 3号店単体完結型：週刊AIプチ書籍の自動統合パブリッシング
# ==========================================
def generate_weekly_book():
    logging.info("=== 3号店：週刊AIプチ書籍自動パブリッシング開始 ===")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logging.warning("GEMINI_API_KEY 未設定のため、書籍生成をスキップします。")
        return

    try:
        json_files = [f for f in os.listdir("data") if f.endswith(".json") and f != "strategy_master.json"]
        if len(json_files) < 5:
            logging.info("記事データが不足しているため、今週の書籍生成をスキップします（最低5記事以上必要）。")
            return

        combined_materials = []
        for j_file in json_files[:15]:
            try:
                with open(os.path.join("data", j_file), "r", encoding="utf-8") as f:
                    art = json.load(f)
                combined_materials.append(f"【戦略テーマ】: {art['title']}\n【本質的背景】: {art['summary_detail']}\n【cocoroの眼】: {art['charo_insight']}")
            except Exception as e:
                logging.warning(f"書籍素材の部分的スキップ: {e}")
                continue

        if not combined_materials:
            return

        client = genai.Client(api_key=api_key)
        model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        materials_text = "\n\n---\n\n".join(combined_materials)

        prompt = f"""
        あなたは、激変するAI社会を生き抜く全人類へ向けた、圧倒的インサイトを誇るライフ戦略ジャーナリストです。
        以下の【直近の戦略ニュースの断片】を美しく紡ぎ合わせ、1つの巨大な人生の羅針盤として描き出した、
        1万文字規模の、読者が膝を打つ「今週の人生・実務戦略深掘りプチ書籍」を執筆してください。

        【執筆構成案】
        第1章：迫り来る激変期の地殻変動とマインドセット
        第2章：主導権（操縦席）を死守するためのスマートな戦略選択
        第3章：中学生でも一瞬で理解できる「これからの生き方」の核心比喩
        第4章：メンタルの調和とココロの平和を保つ具体的方法
        第5章：明日から即座に始動すべき人生のロードマップ

        【厳格ルール】
        - 専門用語やプロンプトハックに逃げず、誰もが納得する「比喩話」を多用して完全に噛み砕いてください。
        - 出力は美しいHTML形式で（h3, p, strong, blockquoteタグを使用）書き出してください。Markdownタグや```htmlといったラッパーはいっさい含めず、純粋なHTML本文のみを出力してください。

        【直近の戦略ニュースの断片】
        {materials_text}
        """

        MAX_RETRIES = 3
        book_html_content = ""
        for attempt in range(MAX_RETRIES):
            try:
                logging.info(f"Gemini API 書籍執筆中... (試行 {attempt + 1}/{MAX_RETRIES})")
                response = client.models.generate_content(model=model_name, contents=prompt)
                if response and response.text:
                    book_html_content = response.text.strip()
                    break
                else:
                    raise ValueError("書籍レスポンスが空です。")
            except Exception as e:
                wait = 2 ** attempt
                time.sleep(wait)
        else:
            logging.error("最大リトライ超過のため書籍生成を中止します。")
            return

        book_html_content = re.sub(r"^```html\s*|\s*```$", "", book_html_content, flags=re.IGNORECASE).strip()
        book_title = f"{datetime.now().strftime('%Y年%m月')} 最新号：AI時代の人生戦略・究極バイブル"
        book_slug = f"weekly-life-book-{datetime.now().strftime('%Y-%m-w%W')}"
        
        success_book = build_page(
            body_template_path="template_book.html",
            title=book_title,
            date_iso=datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
            date_ja=datetime.now().strftime("%Y年%m月%d日"),
            source_url="#",
            source_name="AI Frontier Life 編集部",
            replacements={"{{BOOK_CONTENT}}": book_html_content},
            output_path=os.path.join("books", f"{book_slug}.html"),
            is_article=True,
            slug=book_slug
        )
        if success_book:
            logging.info(f"週刊プチ書籍書き出し成功: {book_slug}")
    except Exception as e:
        logging.error(f"週刊プチ書籍生成エラー: {e}")

# ==========================================
# 11. オーケストレーター（メイン処理）
# ==========================================
def main():
    # 3号店用のライフ戦略系マクロRSSフィードを設定
    RSS_FEEDS = [
        {"url": "https://www.reutersagency.com/feed/?best-topics=tech&post_type=best", "name": "Reuters Tech Macro"},
        {"url": "https://www.cnbc.com/id/19854910/device/rss/rss.html", "name": "CNBC Tech Strategy"}
    ]

    logging.info("--- 3号店：自動巡回タスク開始 ---")
    history = load_history()
    processed_urls = {h["url"] for h in history if "url" in h}
    new_article_created = False
    
    data_files = [f for f in os.listdir("data") if f.endswith(".json") and f != "strategy_master.json"]
    
    # シード用のデモデータ（初期起動テスト用安全ガード）
    if not data_files:
        if os.environ.get("ALLOW_DEMO_SEED", "true").lower() == "true":
            logging.info("データが空のため、3号店用の初期シードを安全に自動実行します。")
            mock_text = "Artificial General Intelligence and automation are shifting the paradigm of white-collar career sustainability. Mental harmony, digital detox, and critical decision-making criteria are becoming the key skills for professional longevity in 2026."
            slug = run_article_generator(mock_text, "https://www.reutersagency.com/feed/", "Reuters Life Seed")
            if slug:
                new_article_created = True

    MAX_PROCESS_PER_RUN = 1
    processed_count = 0

    for feed in RSS_FEEDS:
        fetched_articles = fetch_rss_feed(feed["url"])
        if not fetched_articles:
            continue
        if processed_count >= MAX_PROCESS_PER_RUN:
            break

        for item in fetched_articles:
            if processed_count >= MAX_PROCESS_PER_RUN:
                break
            if item["link"] in processed_urls:
                continue

            if not item["description"] or len(item["description"]) < 100:
                history.append({"url": item["link"], "processed_at": datetime.now().isoformat(), "status": "skipped"})
                processed_urls.add(item["link"])
                continue

            logging.info(f"未処理ニュース検知: {item['title']}")
            print(f"📡 3号店新着検知: {item['title']}")

            full_text = fetch_full_article_text(item["link"])
            if not full_text:
                full_text = item["description"]

            slug = run_article_generator(full_text, item["link"], feed["name"])
            if slug:
                new_article_created = True
                history.append({"url": item["link"], "processed_at": datetime.now().isoformat(), "status": "published"})
                processed_urls.add(item["link"])
                processed_count += 1
                
    if new_article_created:
        generate_weekly_book()
        rebuild_index_and_rotate_storage()
        save_history(history)
    else:
        # 記事が新たに作られなくても、書籍が既にあればバナー等のために安全にインデックスを再構成
        rebuild_index_and_rotate_storage()

if __name__ == '__main__':
    main()
