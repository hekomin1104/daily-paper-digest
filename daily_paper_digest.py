# -*- coding: utf-8 -*-
"""
daily_paper_digest.py
児童精神科・発達心理・育児関連の最新論文を毎日メールで届けるスクリプト

処理の流れ:
  1. PubMed APIで過去14日間の論文を検索
  2. 送信済みPMIDを除外（重複スキップ）
  3. 各論文のAbstractを取得
  4. Gemini Flash APIで日本語要約を生成
  5. HTMLメールを作成してGmail送信
  6. 送信済みPMIDをJSONファイルに保存
"""

import os
import json
import time
import smtplib
import sys
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import xml.etree.ElementTree as ET

import requests
from google import genai

# ─── 設定 ──────────────────────────────────────────────────────────────────

# PubMed E-utilities のベースURL
PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

# 検索クエリ（児童精神科・発達関連のMeSH用語）
TOPIC_QUERY = (
    "(child psychiatry[MH] OR adolescent psychiatry[MH] "
    "OR autism spectrum disorder[MH] "
    "OR attention deficit disorder with hyperactivity[MH] "
    "OR child development[MH] OR parenting[MH] "
    "OR developmental disabilities[MH] "
    "OR child behavior disorders[MH]) AND hasabstract[text] AND free full text[sb]"
)

# ─── ジャーナルリスト（PubMed タイトル略称・IF別）────────────────────────
# IF5未満は除外

# Tier 1: IF 30以上
JOURNALS_TIER1 = [
    "JAMA",              # IF ~120
    "N Engl J Med",      # IF ~96
    "Nat Med",           # IF ~82
    "Lancet",            # IF ~168
    "Lancet Psychiatry", # IF ~64
    "World Psychiatry",  # IF ~73
]

# Tier 2: IF 10〜30
JOURNALS_TIER2 = [
    "JAMA Psychiatry",   # IF ~25
    "Am J Psychiatry",   # IF ~18
    "JAMA Pediatr",      # IF ~15
]

# Tier 3: IF 5〜10
JOURNALS_TIER3 = [
    "Br J Psychiatry",                    # IF ~9
    "J Am Acad Child Adolesc Psychiatry", # IF ~9
    "Pediatrics",                         # IF ~8
    "J Child Psychol Psychiatry",         # IF ~8
    "Eur Child Adolesc Psychiatry",       # IF ~6
    "Psychol Med",                        # IF ~6
    "Dev Med Child Neurol",               # IF ~5
]

# IF5未満は含まない（Child Dev ~4, J Autism Dev Disord ~4 など）

def _build_journal_filter(journals: list[str]) -> str:
    """ジャーナルリストからPubMedフィルタ文字列を生成する。"""
    return "(" + " OR ".join(f'"{j}"[TA]' for j in journals) + ")"

# 1回の検索で取得する最大件数
SEARCH_RETMAX = 30

# 今回のメールで送る最大論文数
MAX_PAPERS_PER_EMAIL = 2

# 送信済みPMIDを保存するファイル
SENT_PAPERS_FILE = "sent_papers.json"

# 送信済みPMIDの最大保持件数（古いものから削除）
MAX_SENT_PMIDS = 500

# 環境変数から各種設定を読み込む
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "y.shinjiro1104@gmail.com")


# ─── Step 1: PubMed検索 ────────────────────────────────────────────────────

def _run_search(query: str, reldate: int | None) -> list[str]:
    """PubMed esearch APIを実行してPMIDリストを返す内部関数。
    reldate=None の場合は期間制限なし・関連度順（引用数重視の Best Match）で検索する。
    """
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": SEARCH_RETMAX,
        "retmode": "json",
    }
    if reldate is not None:
        params["datetype"] = "pdat"
        params["reldate"] = reldate
        params["sort"] = "pub+date"
    else:
        params["sort"] = "relevance"  # Best Match: PubMedが引用数・重要度を加味したスコアで並び替え
    try:
        resp = requests.get(PUBMED_BASE + "esearch.fcgi", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("esearchresult", {}).get("idlist", [])
    except Exception as e:
        print(f"  [エラー] PubMed検索失敗: {e}")
        return []


def search_pubmed() -> tuple[list[str], bool]:
    """
    IFの高い雑誌から順に4段階フォールバック検索。
      Tier1: IF30以上      → 過去1年
      Tier2: IF10以上に拡大 → 過去1年
      Tier3: IF5以上に拡大  → 過去1年
      Tier4: IF5以上・年代不問 → 関連度順（引用数重視）フォールバック
    いずれの段階でも MAX_PAPERS_PER_EMAIL 件以上揃えば次に進まない。
    IF5未満はいかなる場合も使用しない。

    戻り値: (PMIDリスト, Tier4フォールバック使用フラグ)
    """
    print("Step 1: PubMed検索中（IFフィルタ付き4段階）...")

    tiers = [
        ("Tier1 IF30以上・2年以内",           JOURNALS_TIER1,                                    730),
        ("Tier2 IF10以上・2年以内",            JOURNALS_TIER1 + JOURNALS_TIER2,                   730),
        ("Tier3 IF5以上・2年以内",             JOURNALS_TIER1 + JOURNALS_TIER2 + JOURNALS_TIER3,   730),
        ("Tier4 IF5以上・年代不問（引用数重視）", JOURNALS_TIER1 + JOURNALS_TIER2 + JOURNALS_TIER3,  None),
    ]

    pmids: list[str] = []
    used_fallback = False
    for tier_label, journals, reldate in tiers:
        journal_filter = _build_journal_filter(journals)
        query = f"{TOPIC_QUERY} AND {journal_filter}"
        pmids = _run_search(query, reldate=reldate)
        print(f"  → {tier_label}: {len(pmids)}件")
        if len(pmids) >= MAX_PAPERS_PER_EMAIL:
            if reldate is None:
                used_fallback = True
            break

    if not pmids:
        print("  → 全Tierで0件。IF5未満は使用しないため本日は送信なし。")

    return pmids, used_fallback


# ─── Step 2: 重複除外 ──────────────────────────────────────────────────────

def load_sent_pmids() -> list[str]:
    """送信済みPMIDのリストをJSONファイルから読み込む。"""
    if not os.path.exists(SENT_PAPERS_FILE):
        return []
    try:
        with open(SENT_PAPERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def filter_new_pmids(all_pmids: list[str], sent_pmids: list[str]) -> list[str]:
    """
    送信済みPMIDを除外して、未送信のPMIDを最大MAX_PAPERS_PER_EMAIL件返す。
    """
    sent_set = set(sent_pmids)
    new_pmids = [p for p in all_pmids if p not in sent_set]
    print(f"Step 2: 重複除外後 {len(new_pmids)}件（最大{MAX_PAPERS_PER_EMAIL}件送信）")
    return new_pmids[:MAX_PAPERS_PER_EMAIL]


# ─── Step 3: Abstract取得 ──────────────────────────────────────────────────

def fetch_paper_details(pmids: list[str]) -> list[dict]:
    """
    efetch APIでXMLを取得し、各論文の詳細情報を辞書リストで返す。

    各辞書のキー:
      pmid, title, abstract, authors, journal, year
    """
    print(f"Step 3: {len(pmids)}件のAbstractを取得中...")
    if not pmids:
        return []

    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "rettype": "abstract",
        "retmode": "xml",
    }
    try:
        resp = requests.get(PUBMED_BASE + "efetch.fcgi", params=params, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [エラー] Abstract取得失敗: {e}")
        return []

    papers = []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"  [エラー] XMLパース失敗: {e}")
        return []

    for article in root.findall(".//PubmedArticle"):
        try:
            # PMID
            pmid_el = article.find(".//PMID")
            pmid = pmid_el.text.strip() if pmid_el is not None else "unknown"

            # タイトル（テキストのみ、タグ内タグを含む場合も対応）
            title_el = article.find(".//ArticleTitle")
            title = _get_element_text(title_el) if title_el is not None else "(タイトル不明)"

            # Abstract
            abstract_parts = []
            for ab_el in article.findall(".//AbstractText"):
                label = ab_el.get("Label")
                text = _get_element_text(ab_el)
                if text:
                    if label:
                        abstract_parts.append(f"{label}: {text}")
                    else:
                        abstract_parts.append(text)
            abstract = " ".join(abstract_parts) if abstract_parts else "(Abstract なし)"

            # 著者（第一著者のみ）
            first_author = ""
            author_el = article.find(".//AuthorList/Author")
            if author_el is not None:
                last = author_el.findtext("LastName", "")
                fore = author_el.findtext("ForeName", "")
                first_author = f"{last} {fore}".strip() if last else fore

            # 雑誌名（略称）
            journal = article.findtext(".//Journal/ISOAbbreviation", "")
            if not journal:
                journal = article.findtext(".//MedlineTA", "")

            # 出版年
            year = article.findtext(".//PubDate/Year", "")
            if not year:
                medline_date = article.findtext(".//PubDate/MedlineDate", "")
                year = medline_date[:4] if medline_date else ""

            # PMC ID・DOI（全文リンク用）
            pmc_id = ""
            doi = ""
            for aid in article.findall(".//ArticleIdList/ArticleId"):
                id_type = aid.get("IdType", "")
                if id_type == "pmc" and aid.text:
                    pmc_id = aid.text.strip().replace("PMC", "")
                elif id_type == "doi" and aid.text:
                    doi = aid.text.strip()

            papers.append({
                "pmid": pmid,
                "title": title,
                "abstract": abstract,
                "authors": first_author,
                "journal": journal,
                "year": year,
                "pmc_id": pmc_id,
                "doi": doi,
            })
        except Exception as e:
            print(f"  [警告] 論文パース失敗 (PMID={pmid}): {e}")
            continue

    print(f"  → {len(papers)}件の詳細を取得")
    return papers


def _get_element_text(el) -> str:
    """
    ET要素から内部テキスト（子タグのテキストも含む）を再帰的に結合して返す。
    """
    if el is None:
        return ""
    parts = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(_get_element_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts).strip()


# ─── Step 4: Gemini で日本語要約 ───────────────────────────────────────────

def generate_summary(paper: dict) -> str:
    """
    Gemini Flash APIを使って論文の日本語要約を生成する。
    失敗した場合は英語Abstractをそのまま返す。
    """
    if not GEMINI_API_KEY:
        print("  [警告] GEMINI_API_KEY が未設定のため要約をスキップ")
        return paper["abstract"]

    prompt = (
        "You are a child and adolescent psychiatrist reading research papers. "
        "Summarize the following paper in Japanese using exactly this format:\n\n"
        "【タイトル（日本語）】\n"
        "（論文タイトルの日本語訳を1行で）\n\n"
        "【わかったこと】\n"
        "（何を調べて何がわかったか、2〜3文で簡潔に。専門用語は平易な表現に言い換える）\n\n"
        "Do not add any other sections or commentary.\n\n"
        f"Title: {paper['title']}\n"
        f"Abstract: {paper['abstract']}"
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"  [警告] Gemini API エラー (PMID={paper['pmid']}): {type(e).__name__}: {e}")
        print(f"  [警告] Abstractをそのまま使用します")
        return paper["abstract"]


# ─── Step 5: HTMLメール作成・送信 ──────────────────────────────────────────

def build_html_email(papers: list[dict], summaries: list[str], today: date, used_fallback: bool = False) -> str:
    """
    論文カードを並べたHTMLメール本文を作成して返す。
    """
    date_str_header = today.strftime("%Y年%m月%d日")
    date_str_subject = today.strftime("%Y/%m/%d")

    # CSSスタイル
    style = """
    body { font-family: 'Hiragino Sans', 'Meiryo', sans-serif; background: #f4f4f4; margin: 0; padding: 20px; }
    .container { max-width: 700px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
    .header { background: #2c5f8a; color: white; padding: 24px 32px; }
    .header h1 { margin: 0 0 4px 0; font-size: 22px; }
    .header p { margin: 0; font-size: 14px; opacity: 0.85; }
    .body { padding: 24px 32px; }
    .card { border: 1px solid #e0e0e0; border-radius: 6px; margin-bottom: 24px; overflow: hidden; }
    .card-index { background: #2c5f8a; color: white; display: inline-block; padding: 2px 10px; font-weight: bold; font-size: 13px; }
    .card-title { font-size: 16px; font-weight: bold; padding: 12px 16px 4px 16px; color: #1a1a1a; }
    .card-meta { font-size: 12px; color: #888; padding: 0 16px 12px 16px; }
    .card-summary { background: #f9f9f9; padding: 16px; font-size: 14px; line-height: 1.7; white-space: pre-wrap; }
    .card-link { padding: 12px 16px; font-size: 13px; }
    .card-link a { color: #2c5f8a; text-decoration: none; }
    .footer { background: #f0f0f0; color: #888; font-size: 12px; padding: 16px 32px; text-align: center; }
    .no-papers { padding: 40px; text-align: center; color: #666; font-size: 16px; }
    """

    cards_html = ""

    if not papers:
        cards_html = '<div class="no-papers">本日は新着論文が見つかりませんでした。</div>'
    else:
        for i, (paper, summary) in enumerate(zip(papers, summaries), start=1):
            meta = []
            if paper["authors"]:
                meta.append(f"著者: {paper['authors']} et al.")
            if paper["journal"]:
                meta.append(f"雑誌: {paper['journal']}")
            if paper["year"]:
                meta.append(paper["year"])
            meta_str = " | ".join(meta)

            pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{paper['pmid']}/"

            # 全文リンク: PMC → DOI → PubMed の優先順
            if paper.get("pmc_id"):
                fulltext_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{paper['pmc_id']}/"
                fulltext_label = "📄 全文を読む（PMC）"
            elif paper.get("doi"):
                fulltext_url = f"https://doi.org/{paper['doi']}"
                fulltext_label = "📄 全文を読む（DOI）"
            else:
                fulltext_url = pubmed_url
                fulltext_label = "🔗 PubMed"

            cards_html += f"""
            <div class="card">
              <div style="padding: 12px 16px 0 16px;">
                <span class="card-index">【{i}】</span>
              </div>
              <div class="card-title">{_html_escape(paper['title'])}</div>
              <div class="card-meta">{_html_escape(meta_str)}</div>
              <div class="card-summary">{_html_escape(summary)}</div>
              <div class="card-link">
                <a href="{fulltext_url}" style="font-weight:bold;">{fulltext_label}</a>
                &nbsp;&nbsp;
                <a href="{pubmed_url}" style="color:#888; font-size:12px;">（PubMed抄録）</a>
              </div>
            </div>
            """

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>{style}</style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>📚 今日の論文ダイジェスト</h1>
      <p>{date_str_header} | 児童精神科・発達心理・育児{"　※引用数重視・年代不問の論文です" if used_fallback else ""}</p>
    </div>
    <div class="body">
      {cards_html}
    </div>
    <div class="footer">
      このメールは自動送信されています。
    </div>
  </div>
</body>
</html>"""

    return html, date_str_subject


def _html_escape(text: str) -> str:
    """HTMLエスケープ（最小限）"""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def send_email(html_body: str, subject: str) -> None:
    """
    GmailのSMTPでHTMLメールを送信する。
    失敗した場合は標準出力にエラーを出してexit(1)する。
    """
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("[エラー] GMAIL_USER または GMAIL_APP_PASSWORD が未設定です。")
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL

    # テキスト版（フォールバック）
    plain_text = "このメールはHTMLメールです。HTMLに対応したメールクライアントでご覧ください。"
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        print(f"Step 5: メール送信中 → {RECIPIENT_EMAIL}")
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_bytes())
        print("  → 送信完了")
    except smtplib.SMTPAuthenticationError:
        print("[エラー] Gmail認証失敗。GMAIL_USER/GMAIL_APP_PASSWORD を確認してください。")
        sys.exit(1)
    except Exception as e:
        print(f"[エラー] メール送信失敗: {e}")
        sys.exit(1)


# ─── Step 6: 送信済みPMID保存 ──────────────────────────────────────────────

def save_sent_pmids(existing: list[str], new_pmids: list[str]) -> None:
    """
    送信済みPMIDリストに新規PMIDを追記してJSONに保存する。
    合計がMAX_SENT_PMIDSを超えた場合は古いものから削除する。
    """
    combined = existing + new_pmids
    # 重複を除きながら順序を維持（既存→新規の順）
    seen = set()
    deduped = []
    for p in combined:
        if p not in seen:
            seen.add(p)
            deduped.append(p)

    # 最大件数を超えたら先頭（古い方）から削除
    if len(deduped) > MAX_SENT_PMIDS:
        deduped = deduped[-MAX_SENT_PMIDS:]

    with open(SENT_PAPERS_FILE, "w", encoding="utf-8") as f:
        json.dump(deduped, f, ensure_ascii=False, indent=2)
    print(f"Step 6: sent_papers.json を更新（合計{len(deduped)}件）")


# ─── メイン処理 ────────────────────────────────────────────────────────────

def main():
    today = date.today()
    print(f"=== 論文ダイジェスト実行: {today} ===")

    # Step 1: PubMed検索
    all_pmids, used_fallback = search_pubmed()

    # Step 2: 重複除外
    sent_pmids = load_sent_pmids()
    target_pmids = filter_new_pmids(all_pmids, sent_pmids)

    # 新着論文なしの場合
    if not target_pmids:
        print("新着論文なし → 通知メールを送信します")
        html, date_str = build_html_email([], [], today)
        subject = f"📚 論文ダイジェスト {date_str} | 児童精神科・発達（新着なし）"
        send_email(html, subject)
        return

    # Step 3: Abstract取得
    papers = fetch_paper_details(target_pmids)

    if not papers:
        print("[警告] Abstract取得失敗 → 通知メールを送信します")
        html, date_str = build_html_email([], [], today)
        subject = f"📚 論文ダイジェスト {date_str} | 児童精神科・発達（取得失敗）"
        send_email(html, subject)
        return

    # Step 4: Gemini で日本語要約
    print("Step 4: Gemini で日本語要約を生成中...")
    summaries = []
    for i, paper in enumerate(papers, start=1):
        print(f"  [{i}/{len(papers)}] PMID={paper['pmid']} を要約中...")
        summary = generate_summary(paper)
        summaries.append(summary)
        # API レート制限を避けるため少し待機
        if i < len(papers):
            time.sleep(1)

    # Step 5: HTMLメール作成・送信
    html, date_str = build_html_email(papers, summaries, today, used_fallback=used_fallback)
    fallback_tag = "（引用数重視）" if used_fallback else ""
    subject = f"📚 論文ダイジェスト {date_str} | 児童精神科・発達{fallback_tag}"
    send_email(html, subject)

    # Step 6: 送信済みPMIDを保存
    sent_new_pmids = [p["pmid"] for p in papers]
    save_sent_pmids(sent_pmids, sent_new_pmids)

    print("=== 完了 ===")


if __name__ == "__main__":
    main()
