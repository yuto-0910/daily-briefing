import os
import json
import re
import time
from datetime import datetime, timedelta, timezone
from google import genai
from google.genai import types
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv

# .envファイルから設定（APIキーなど）を読み込みます
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIEFINGS_DIR = os.path.join(BASE_DIR, 'briefings')
load_dotenv(os.path.join(BASE_DIR, '.env'))

JST = timezone(timedelta(hours=9))

# Gemini APIの最新クライアント設定
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Gmail APIの利用範囲（読み取り専用）を設定
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def generate_with_backoff(client, model, contents, config, max_retries=5):
    """Exponential Backoffで503/429エラー時に自動リトライする"""
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )
        except Exception as e:
            if any(code in str(e) for code in ['503', '429', 'UNAVAILABLE', 'RESOURCE_EXHAUSTED']):
                if attempt < max_retries - 1:
                    wait = (2 ** attempt) + 1
                    print(f"APIエラー({e})。{wait}秒後にリトライ ({attempt + 1}/{max_retries})")
                    time.sleep(wait)
                else:
                    raise Exception(f"最大リトライ回数({max_retries}回)超過: {e}")
            else:
                raise e

def get_gmail_service():
    """Gmail APIに接続するための認証処理を行います"""
    creds = None
    token_path = os.path.join(BASE_DIR, 'token.json')
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", os.path.join(BASE_DIR, "credentials.json"))
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
            
    return build('gmail', 'v1', credentials=creds)

def fetch_emails(service):
    """JST基準で昨日のメールを取得して、件名・本文・URL・受信日を抽出します"""
    now_jst = datetime.now(JST)
    yesterday_jst = now_jst - timedelta(days=1)
    start = yesterday_jst.replace(hour=0, minute=0, second=0, microsecond=0)
    end = yesterday_jst.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    # Unixタイムスタンプを使用してJSTの1日分を正確に指定
    query = f'after:{int(start.timestamp())} before:{int(end.timestamp())}'
    
    results = service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])
    
    email_data = []
    for msg in messages:
        m = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        payload = m.get('payload', {})
        headers = payload.get('headers', [])
        subject = ""
        sender = ""
        for h in headers:
            if h['name'] == 'Subject':
                subject = h['value']
            if h['name'] == 'From':
                sender = h['value']
        
        # 受信日時（JST）の取得
        internal_date_ms = int(m.get('internalDate', 0))
        email_date = datetime.fromtimestamp(internal_date_ms / 1000, JST).strftime('%Y-%m-%d')
        
        snippet = m.get('snippet', '')
        
        # URLの抽出（フィルタリング条件維持）
        raw_urls = re.findall(r'https://[^\s<>"]+', snippet)
        filtered_urls = []
        exclude_patterns = ['github.io', 'google.com/url', 'mail.google.com', 'unsubscribe', 'optout', 'opt-out', 'privacy-policy', 'delivery-preferences']
        for url in raw_urls:
            if not any(pattern in url.lower() for pattern in exclude_patterns):
                filtered_urls.append(url)
        
        email_data.append({
            "message_id": msg['id'],
            "subject": subject,
            "sender": sender,
            "snippet": snippet,
            "urls": filtered_urls,
            "date": email_date
        })
    
    return email_data

def summarize_emails(emails):
    """Gemini 2.5 Flash でメールを3カテゴリに分類・要約する"""

    prompt = f"""
以下のメールリストを分析し、指定されたJSON形式のみで返してください。
前置き・説明文・コードブロック（```json等）は一切不要です。JSONのみを返してください。

## 分類ルール

### カテゴリ1: important（重要メール）
以下のいずれかに該当するメールを分類する：
- 宛名・本文にYuto、青木、Aoki等の個人名が含まれる
- アラート・エラー・警告・障害・セキュリティ通知
- インボイス・請求書・支払い・領収書・決済・クレジット関連
- 期限・締め切り・リマインダー・要対応
- 契約・申込・登録完了・解約通知

### カテゴリ2: news（ニュース・メルマガ）
以下のいずれかに該当するメールを分類する：
- ニュースメディア・メルマガ・定期配信
- 日経・Bloomberg・Forbes・Reuters・NewsPicks・WSJ等からの配信
- テック・ビジネス・経済・投資関連の定期メール

### カテゴリ3: other（その他）
- 上記2カテゴリのいずれにも該当しないメール
- マーケティングメール・広告・セール情報・SNS通知等

## 各メールに必ず含める情報

- `message_id`: メールのID（後でGmailリンク生成に使用）
- `subject`: メールの件名（原文のまま変更しない）
- `sender`: 送信者
- `date`: メール受信日（YYYY-MM-DD形式）
- `category`: "important" / "news" / "other" のいずれか
- `summary`: メール内容の要点を日本語で3点の配列（各40字以内）

## 絶対に守るルール

- 全メールを漏れなくリストアップすること（件数を減らさない）
- subjectは原文のまま（翻訳・省略・変更禁止）
- dateは必ず前日日付（YYYY-MM-DD）と一致していること
- summaryは必ず3点の配列にすること（2点・4点は禁止）
- JSONのみを返すこと（他のテキスト一切不要）

## 分析対象データ

{json.dumps(emails, ensure_ascii=False)}

## 出力形式

{{
  "date": "YYYY-MM-DD",
  "important": [
    {{
      "message_id": "メールID",
      "subject": "件名（原文のまま）",
      "sender": "送信者",
      "date": "YYYY-MM-DD",
      "category": "important",
      "summary": ["要点1", "要点2", "要点3"]
    }}
  ],
  "news": [
    {{
      "message_id": "メールID",
      "subject": "件名（原文のまま）",
      "sender": "送信者",
      "date": "YYYY-MM-DD",
      "category": "news",
      "summary": ["要点1", "要点2", "要点3"]
    }}
  ],
  "other": [
    {{
      "message_id": "メールID",
      "subject": "件名（原文のまま）",
      "sender": "送信者",
      "date": "YYYY-MM-DD",
      "category": "other",
      "summary": ["要点1", "要点2", "要点3"]
    }}
  ]
}}
"""

    response = generate_with_backoff(
        client=client,
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )
    )

    text = response.text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    return json.loads(text)

def generate_html(date_str, data):
    """分類済みメールをHTMLに書き出す"""

    def build_email_items(email_list):
        """メールリストをHTMLに変換する"""
        html = ""
        for e in email_list:
            gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{e.get('message_id', '')}"
            subject_html = f'<a href="{gmail_url}" target="_blank" style="font-weight:bold; font-size:1.05rem; text-decoration:none; color:#1a73e8;">{e["subject"]}</a>'
            sender_html = f'<small style="color:#666;">{e["sender"]} | {e["date"]}</small>'
            summary_points = "".join([f"<li>{p}</li>" for p in e.get("summary", [])])
            html += f'''
            <div class="email-item">
                {subject_html}<br>
                {sender_html}
                <ul style="margin-top:8px; padding-left:20px; font-size:0.9rem; color:#444;">
                    {summary_points}
                </ul>
            </div>'''
        return html if html else '<p style="color:#999; font-size:0.9rem;">該当メールなし</p>'

    important_html = build_email_items(data.get("important", []))
    news_html = build_email_items(data.get("news", []))
    other_html = build_email_items(data.get("other", []))

    important_count = len(data.get("important", []))
    news_count = len(data.get("news", []))
    other_count = len(data.get("other", []))
    total_count = important_count + news_count + other_count

    template = f'''<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Daily Briefing - {date_str}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f9f9f9;
        }}
        .header {{
            background: #fff;
            padding: 20px 24px;
            border-radius: 12px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.05);
            margin-bottom: 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .header h1 {{
            margin: 0;
            font-size: 1.4rem;
            color: #333;
        }}
        .badge {{
            background: #e8f0fe;
            color: #1a73e8;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: bold;
        }}
        .section {{
            background: #fff;
            padding: 24px;
            border-radius: 12px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.05);
            margin-bottom: 24px;
        }}
        .section-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 2px solid #f0f0f0;
        }}
        h2 {{
            margin: 0;
            font-size: 1.1rem;
            color: #333;
        }}
        .count-badge {{
            background: #f0f0f0;
            color: #666;
            padding: 2px 10px;
            border-radius: 20px;
            font-size: 0.8rem;
        }}
        .important .section-header {{ border-bottom-color: #dc2626; }}
        .important h2 {{ color: #dc2626; }}
        .news .section-header {{ border-bottom-color: #1a73e8; }}
        .news h2 {{ color: #1a73e8; }}
        .other .section-header {{ border-bottom-color: #999; }}
        .other h2 {{ color: #666; }}
        .email-item {{
            border-bottom: 1px solid #eee;
            padding: 14px 0;
        }}
        .email-item:last-child {{ border-bottom: none; }}
        .email-item a:hover {{ text-decoration: underline !important; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>📅 {date_str} の受信メール</h1>
        <span class="badge">合計 {total_count} 件</span>
    </div>

    <div class="section important">
        <div class="section-header">
            <h2>🚨 重要メール</h2>
            <span class="count-badge">{important_count} 件</span>
        </div>
        {important_html}
    </div>

    <div class="section news">
        <div class="section-header">
            <h2>📰 ニュース・メルマガ</h2>
            <span class="count-badge">{news_count} 件</span>
        </div>
        {news_html}
    </div>

    <div class="section other">
        <div class="section-header">
            <h2>📋 その他</h2>
            <span class="count-badge">{other_count} 件</span>
        </div>
        {other_html}
    </div>
</body>
</html>'''

    file_path = os.path.join(BRIEFINGS_DIR, f"{date_str}.html")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(template)

def update_index():
    """過去7日分のHTMLを探して、トップページを更新します"""
    files = sorted([f for f in os.listdir(BRIEFINGS_DIR) if f.endswith(".html")], reverse=True)[:7]
    
    tab_buttons = []
    tab_contents = []
    
    for i, f in enumerate(files):
        date_label = f.replace(".html", "")
        active_class = "active" if i == 0 else ""
        tab_buttons.append(f'<button class="tab-btn {active_class}" onclick="showTab(\'tab-{date_label}\', this)">{date_label}</button>')
        tab_contents.append(f'<div id="tab-{date_label}" class="tab-content {active_class}"><iframe src="briefings/{f}" width="100%" height="800px"></iframe></div>')

    index_html = f'''
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Daily Briefing Dashboard</title>
        <style>
            body {{ font-family: sans-serif; margin: 0; padding: 0; display: flex; flex-direction: column; height: 100vh; background: #eee; }}
            .tabs {{ background: #333; overflow-x: auto; white-space: nowrap; padding: 10px; display: flex; gap: 8px; }}
            .tab-btn {{ background: #555; color: white; border: none; padding: 8px 16px; cursor: pointer; border-radius: 6px; transition: 0.3s; }}
            .tab-btn.active {{ background: #007bff; font-weight: bold; }}
            .tab-content {{ display: none; flex-grow: 1; }}
            .tab-content.active {{ display: block; }}
            iframe {{ border: none; width: 100%; height: 100%; background: white; }}
        </style>
        <script>
            function showTab(id, btn) {{
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.getElementById(id).classList.add('active');
                btn.classList.add('active');
            }}
        </script>
    </head>
    <body>
        <div class="tabs">
            {"".join(tab_buttons)}
        </div>
        {"".join(tab_contents)}
    </body>
    </html>
    '''
    with open(os.path.join(BASE_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)

if __name__ == "__main__":
    try:
        service = get_gmail_service()
        emails = fetch_emails(service)
        if emails:
            summary_data = summarize_emails(emails)
            yesterday_jst = datetime.now(JST) - timedelta(days=1)
            yesterday_str = yesterday_jst.strftime('%Y-%m-%d')
            generate_html(yesterday_str, summary_data)
            update_index()
            important_count = len(summary_data.get("important", []))
            news_count = len(summary_data.get("news", []))
            other_count = len(summary_data.get("other", []))
            print(f"Success: {yesterday_str} / 重要:{important_count}件 ニュース:{news_count}件 その他:{other_count}件")
        else:
            print("No emails found for yesterday.")
    except Exception as e:
        print(f"Error occurred: {e}")
        raise
