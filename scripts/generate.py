import os
import datetime
import json
import re
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

# Gemini APIの最新クライアント設定
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Gmail APIの利用範囲（読み取り専用）を設定
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

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
    """昨日のメールを取得して、件名・本文・URLを抽出します"""
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y/%m/%d')
    today = datetime.date.today().strftime('%Y/%m/%d')
    query = f'after:{yesterday} before:{today}'
    
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
        
        snippet = m.get('snippet', '')
        
        # URLの抽出（フィルタリング条件維持）
        raw_urls = re.findall(r'https://[^\s<>"]+', snippet)
        filtered_urls = []
        exclude_patterns = ['github.io', 'google.com/url', 'mail.google.com', 'unsubscribe', 'optout', 'opt-out', 'privacy-policy', 'delivery-preferences']
        for url in raw_urls:
            if not any(pattern in url.lower() for pattern in exclude_patterns):
                filtered_urls.append(url)
        
        email_data.append({
            "subject": subject, 
            "sender": sender, 
            "snippet": snippet,
            "urls": filtered_urls
        })
    
    return email_data

def summarize_emails(emails):
    """Gemini 2.5 Flash と Google 検索グラウンディングで分析・要約します"""
    prompt = f"""
    以下のメールリストを分析し、指定されたJSON形式のみで結果を返してください。
    
    指示:
    - 各記事タイトルについて、Google検索でその記事の個別ページURLを必ず探し出してください。
    - メール本文から抽出したURLはトラッキング用のため最終出力には使用しないでください。
    - Google検索で見つかった「実際の記事個別ページのURL」を使用してください。見つからない場合はnullにしてください。
    - メールに含まれる記事は省略せずすべて抽出してください。
    - InstagramやLINEなどSNS通知・マーケティングメール・広告メールは除外してください。
    - ニュースサイト・ブログ・メディアからのメールの記事はすべて漏れなくリストアップしてください。
    - 各ニュースに対し、検索結果に基づいた箇条書き5点の要約を作成（日本語）。
    - 各ニュースのソース名（Nikkei, Forbes等）を自動判別してください。
    - 複数ソースで言及されている注目ニュースは「is_hot: true」に設定してください。

    分析対象データ:
    {json.dumps(emails, ensure_ascii=False)}
    
    出力形式:
    {{
      "important": [
        {{"subject": "...", "sender": "...", "summary": "...", "priority": "High/Medium/Low"}}
      ],
      "news": [
        {{
          "title": "記事タイトル",
          "url": "実際の記事URL or null",
          "source": "ソース名",
          "is_hot": true/false,
          "summary": ["要点1", "要点2", "要点3", "要点4", "要点5"]
        }}
      ]
    }}
    """
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )
    )
    result = response.text
    
    text = result.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    
    return json.loads(text)

def generate_html(date_str, data):
    """分析結果をHTMLに書き出します"""
    
    important_html = ""
    for e in data.get("important", []):
        important_html += f'''
        <div class="email-item">
            <strong>{e["subject"]}</strong> 
            <span class="tag tag-{e["priority"].lower()}">{e["priority"]}</span><br>
            <small>{e["sender"]}</small>
            <p>{e["summary"]}</p>
        </div>'''

    # ホットニュースセクション（箇条書き要約を追加）
    hot_news_html = ""
    for n in data.get("news", []):
        if n.get("is_hot"):
            summary_points = "".join([f'<li>{p}</li>' for p in n.get("summary", [])])
            link_html = f'<a href="{n["url"]}" target="_blank">{n["title"]}</a>' if n.get("url") else n["title"]
            
            hot_news_html += f'''
            <div class="hot-news">
                <div style="margin-bottom: 8px;">🔥 【{n["source"]}】 <strong>{link_html}</strong></div>
                <ul style="margin: 0; padding-left: 20px; font-size: 0.9rem; font-weight: normal; color: #92400e;">
                    {summary_points}
                </ul>
            </div>'''

    # 通常ニュース一覧
    news_list_html = ""
    for n in data.get("news", []):
        if not n.get("is_hot"):
            summary_points = "".join([f'<li>{p}</li>' for p in n.get("summary", [])])
            title_html = f'<a href="{n["url"]}" target="_blank" style="font-weight: bold; font-size: 1.1rem; text-decoration: none; color: #007bff;">{n["title"]}</a>' if n.get("url") else f'<span style="font-weight: bold; font-size: 1.1rem; color: #333;">{n["title"]}</span>'
            
            news_list_html += f'''
            <div class="email-item">
                <span class="source-badge">[{n["source"]}]</span> 
                {title_html}
                <ul style="margin-top: 8px; padding-left: 20px; font-size: 0.9rem; color: #444;">
                    {summary_points}
                </ul>
            </div>'''

    template = f'''
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Daily Briefing - {date_str}</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; background-color: #f9f9f9; }}
            .section {{ background: #fff; padding: 24px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); margin-bottom: 24px; }}
            h2 {{ border-left: 4px solid #007bff; padding-left: 12px; color: #007bff; font-size: 1.25rem; margin-top: 0; }}
            .email-item {{ border-bottom: 1px solid #eee; padding: 16px 0; }}
            .email-item:last-child {{ border-bottom: none; }}
            .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; margin-left: 8px; vertical-align: middle; }}
            .tag-high {{ background-color: #fee2e2; color: #dc2626; }}
            .tag-medium {{ background-color: #fef3c7; color: #d97706; }}
            .tag-low {{ background-color: #dcfce7; color: #16a34a; }}
            .source-badge {{ background: #e2e8f0; color: #475569; padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: bold; text-transform: uppercase; }}
            .hot-news {{ background-color: #fffbeb; border: 1px solid #fde68a; padding: 16px; border-radius: 8px; color: #92400e; margin-bottom: 12px; }}
            .hot-news a {{ color: #92400e; text-decoration: underline; }}
        </style>
    </head>
    <body>
        <h1>📅 Briefing: {date_str}</h1>
        
        <div class="section">
            <h2>🚨 重要メール</h2>
            {important_html}
        </div>

        <div class="section">
            <h2>🔥 ホットニュース</h2>
            {hot_news_html}
        </div>

        <div class="section">
            <h2>📰 ニュース要約一覧</h2>
            {news_list_html}
        </div>
    </body>
    </html>
    '''
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
            yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
            generate_html(yesterday_str, summary_data)
            update_index()
            print(f"Success: Briefing for {yesterday_str} generated with hot news summaries.")
        else:
            print("No emails found for yesterday.")
    except Exception as e:
        print(f"Error occurred: {e}")
