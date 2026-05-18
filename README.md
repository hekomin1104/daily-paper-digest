# 毎日論文ダイジェスト自動送信システム

PubMedで児童精神科・発達心理・育児関連の最新論文を毎日自動取得し、Gemini AIが日本語で要約したメールを毎朝7時（日本時間）に送信するシステムです。

---

## セットアップ手順

### 1. GitHubにプライベートリポジトリを作成してpushする

1. [GitHub](https://github.com/) にログインし、右上の「+」→「New repository」をクリック
2. Repository name に好きな名前（例: `daily-paper-digest`）を入力
3. **「Private」を必ず選択**（Public にしないこと）
4. 「Create repository」をクリック
5. 画面に表示されるコマンドに従って、このフォルダの内容をpushしてください

---

### 2. Gemini APIキーを取得する

1. [Google AI Studio](https://aistudio.google.com/) にアクセス（Googleアカウントでログイン）
2. 左上の「Get API key」→「Create API key」をクリック
3. 表示されたキー（`AIza...` で始まる文字列）をコピーして安全な場所に保存

---

### 3. GmailのアプリパスワードはGmailアカウントで取得する

> 通常のGmailパスワードは使えません。専用の「アプリパスワード」が必要です。

1. [Googleアカウント管理](https://myaccount.google.com/) にアクセス
2. 左メニューから「セキュリティ」をクリック
3. 「Googleへのログイン」セクションの **「2段階認証プロセス」** を有効にする（まだの場合）
4. 2段階認証を有効にしたら、同じ「セキュリティ」ページを下にスクロールして **「アプリパスワード」** をクリック
5. 「アプリを選択」→「その他（名前を入力）」→ 好きな名前（例: `Paper Digest`）を入力
6. 「生成」をクリック → 16文字のパスワードが表示されるのでコピーして保存

---

### 4. GitHub Secretsに設定する

GitHubのリポジトリページで **Settings → Secrets and variables → Actions → New repository secret** を開き、以下の4つを登録してください。

| Secret名 | 値 |
|---|---|
| `GEMINI_API_KEY` | 手順2で取得したGemini APIキー |
| `GMAIL_USER` | 送信元のGmailアドレス（例: `yourname@gmail.com`）|
| `GMAIL_APP_PASSWORD` | 手順3で取得した16文字のアプリパスワード（スペースなし）|
| `RECIPIENT_EMAIL` | メールを受け取りたいアドレス（別のアドレスでも可）|

---

### 5. GitHub Actionsで動作確認する（手動実行）

1. GitHubのリポジトリページ上部の **「Actions」** タブをクリック
2. 左側のリストから **「Daily Paper Digest」** を選択
3. 右側の **「Run workflow」** ボタンをクリック → 「Run workflow」で手動実行
4. 数分後に実行結果が表示されます（緑のチェックマーク = 成功）
5. 指定したメールアドレスにメールが届いていれば設定完了です

---

## 自動実行のスケジュール

設定は変更なしで **毎朝7:00（日本時間）** に自動送信されます。

送信時刻を変更したい場合は `.github/workflows/daily_digest.yml` の `cron` を書き換えてください。

```
cron: '0 22 * * *'
```
↑ の数字は「UTC時刻」です。JST = UTC + 9時間なので、22:00 UTC = 翌朝 07:00 JST になります。

| 送りたい時刻（JST） | cronに書く値（UTC） |
|---|---|
| 毎朝 6:00 | `0 21 * * *` |
| 毎朝 7:00 | `0 22 * * *`（デフォルト）|
| 毎朝 8:00 | `0 23 * * *` |

---

## よくある質問

**Q. メールが届かない場合は？**
→ ActionsタブでエラーログをConfirmし、Secretsの設定ミスがないか確認してください。GmailのアプリパスワードはスペースなしでOKです。

**Q. 論文数を増やしたい/減らしたい場合は？**
→ `daily_paper_digest.py` の `MAX_PAPERS_PER_EMAIL = 5` の数字を変更してください。

**Q. 検索条件を変えたい場合は？**
→ `daily_paper_digest.py` の `SEARCH_QUERY` を変更してください（PubMedのMeSH用語を参照）。

---

## ファイル構成

```
daily-paper-digest/
├── daily_paper_digest.py       # メインスクリプト
├── sent_papers.json            # 送信済みPMID記録（自動更新）
├── requirements.txt            # Pythonパッケージ一覧
├── README.md                   # このファイル
└── .github/
    └── workflows/
        └── daily_digest.yml    # GitHub Actions設定
```
