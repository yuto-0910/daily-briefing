import os
import datetime
import json
import google.generativeai as genai
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv

# .envファイルから設定（APIキーなど）を読み込みます
load_dotenv()

# Gemini APIの初期設定
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Gmail APIの利用範囲（読み取り専用）を設定
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def get_gmail_service():
    """Gmail APIに接続するための認証処理を行います"""
    creds = None
    # すでに認証済みの場合は token.json を利用します
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    # 認証が必要な場合、または期限切れの場合は再認証します
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # credentials.json（Google Cloud Consoleからダウンロードしたもの）が必要です
            creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        # 次回のために認証情報を保存します
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
            
    return build('gmail', 'v1', credentials=creds)

def fetch_emails(service):
    """昨日のメールを取得して、件名と本文の冒頭をリストにします"""
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
        
        snippet = m.get('snippet', '')[:200]
        email_data.append({"subject": subject, "sender": sender, "snippet": snippet})
    
    return email_data

def summarize_emails(emails):
    """Gemini APIを使って、メールを重要度とニュースに分類・要約します"""
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    # AIへの指示書（プロンプト）です。JSON形式で出力するように指定しています。
    prompt = f"""
    以下のメールリストを分析し、JSON形式のみで結果を返してください。
    
    分析対象メール:
    {json.dumps(emails, ensure_ascii=False)}
    
    要件:
    1. 「重要メール」: 請求書、返信が必要な個人宛、システムアラートを抽出。
    2. 「ニュースメール」: メルマガ等から記事タイトル一覧を作成。
    3. 重複しているニュースは「is_hot: true」に設定してください。
    
    出力形式:
    {{
      "important": [
        {{"subject": "...", "sender": "...", "summary": "...", "priority": "High/Medium/Low"}}
      ],
      "news": [
        {{"source": "...", "title": "...", "is_hot": true}}
      ]
    }}
    """
    
    response = model.generate_content(prompt)
    text = response.text.strip()
    # Markdownのコードブロック（```json ... ```）を剥ぎ取ります
    if text.startswith("```json"):
        text = text[7:-3].strip()
    return json.loads(text)

def generate_html(date_str, data):
    """分析結果をもとに、きれいなレポートHTMLを作成します"""
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
            .email-item {{ border-bottom: 1px solid #eee; padding: 12px 0; }}
            .email-item:last-child {{ border-bottom: none; }}
            .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; margin-left: 8px; vertical-align: middle; }}
            .tag-high {{ background-color: #fee2e2; color: #dc2626; }}
            .tag-medium {{ background-color: #fef3c7; color: #d97706; }}
            .tag-low {{ background-color: #dcfce7; color: #16a34a; }}
            .hot-news {{ background-color: #fffbeb; border: 1px solid #fde68a; padding: 10px; border-radius: 6px; color: #92400e; font-weight: bold; margin-bottom: 8px; }}
            .source {{ color: #6b7280; font-size: 0.85rem; font-weight: normal; }}
        </style>
    </head>
    <body>
        <h1>📅 Briefing: {date_str}</h1>
        
        <div class="section">
            <h2>🚨 重要メール</h2>
            {"".join([f'<div class="email-item"><strong>{e["subject"]}</strong> <span class="tag tag-{e["priority"].lower()}">{e["priority"]}</span><br><small>{e["sender"]}</small><p>{e["summary"]}</p></div>' for e in data.get("important", [])])}
        </div>

        <div class="section">
            <h2>🔥 ホットニュース</h2>
            {"".join([f'<div class="hot-news">【{n["source"]}】 {n["title"]}</div>' for n in data.get("news", []) if n.get("is_hot")])}
        </div>

        <div class="section">
            <h2>📰 ニュース一覧</h2>
            {"".join([f'<div class="email-item"><span class="source">[{n["source"]}]</span> {n["title"]}</div>' for n in data.get("news", []) if not n.get("is_hot")])}
        </div>
    </body>
    </html>
    '''
    file_path = f"briefings/{date_str}.html"
    # briefingsフォルダに保存します
    with open(f"/Users/yuto/daily-briefing/{file_path}", "w", encoding="utf-8") as f:
        f.write(template)

def update_index():
    """過去7日分のHTMLを探して、トップページを更新します"""
    briefing_dir = "/Users/yuto/daily-briefing/briefings"
    files = sorted([f for f in os.listdir(briefing_dir) if f.endswith(".html")], reverse=True)[:7]
    
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
    with open("/Users/yuto/daily-briefing/index.html", "w", encoding="utf-8") as f:
        f.write(index_html)

if __name__ == "__main__":
    # スクリプトを実行した際のメイン処理
    try:
        service = get_gmail_service()
        emails = fetch_emails(service)
        if emails:
            summary_data = summarize_emails(emails)
            yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
            generate_html(yesterday_str, summary_data)
            update_index()
            print(f"Success: Briefing for {yesterday_str} generated.")
        else:
            print("No emails found for yesterday.")
    except Exception as e:
        print(f"Error occurred: {e}")
