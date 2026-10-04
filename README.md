# FRUSTWAY SEO Crawler

ブラウザで使えるSEO監査用クローラーです。

## v1.9 Browser Release

主な機能：

- HTTP Status / Content-Type
- Indexability
- Title / Description
- Canonical
- H1、必要に応じてH1〜H6
- Crawl Depth
- Internal Inlinks / Outlinks
- Anchor Text
- Link Issues
- Site Structure
- CSV / Excel出力
- クロール進捗
- Stop Crawl
- 部分クロール結果の表示

## Browser版の公開対策

v1.9では、公開Webアプリとして使うために以下を追加しています。

- JobごとのSQLite / CSV分離
- URLの `?job=` による同一Job復元
- 古いJobの自動削除（デフォルト24時間）
- Browser版は最大5,000 URL / Job
- Browser版はDelay最低0.2秒
- Browser版は本文保存OFF
- Browser版は外部リンク保存OFF
- localhost / private IP / link-local等を拒否
- 80 / 443以外のポートを拒否
- 同時クロール数を制限（デフォルト2）

> SSRF対策はアプリケーション層の軽減策です。本格的な本番運用では、クラウド側のネットワーク・egress制御も併用してください。

## ローカル起動

Python 3.12以上推奨。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

ブラウザ版と同じ制限で起動します。

### Local Mode（最大50,000 URL）

Mac/Linux:

```bash
FRUSTWAY_PUBLIC_MODE=0 streamlit run app.py
```

Local Modeでは最大50,000 URLを選択できます。

## GitHubへアップロード

例：

```bash
git init
git add .
git commit -m "FRUSTWAY SEO Crawler v1.9"
git branch -M main
git remote add origin <YOUR_GITHUB_REPOSITORY>
git push -u origin main
```

`jobs/`、SQLite、CSV、Secretsは `.gitignore` で除外されています。

## Streamlit Community Cloudへβ公開

GitHubリポジトリを接続し、エントリーポイントに `app.py` を指定してデプロイします。

Browser版はデフォルトで以下の設定です。

```text
Max pages: 5,000
Job retention: 24 hours
Concurrent crawls: 2
Minimum delay: 0.2 sec
```

設定値は環境変数で変更できます。

```text
FRUSTWAY_PUBLIC_MODE=1
FRUSTWAY_PUBLIC_MAX_PAGES=5000
FRUSTWAY_JOB_TTL_HOURS=24
FRUSTWAY_MAX_CONCURRENT_CRAWLS=2
FRUSTWAY_JOBS_ROOT=jobs
```

## Jobについて

アクセス時にランダムなJob IDを発行し、URLへ次のように付与します。

```text
?job=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

同じURLを再読み込みすると、同じJobのDB / CSV / クロール結果を復元します。

`New Job` を押すと新しいJobへ切り替わります。

### 注意

Streamlit Community Cloudなど一時ファイルシステムを使うホスティングでは、インスタンス再起動時にJobデータが消える可能性があります。

永続的なクロール履歴が必要になった段階で、PostgreSQL / オブジェクトストレージ等へ移行します。

## 大規模サイト

Browser版v1.9は最大5,000 URLです。

ローカルStandard Modeは最大50,000 URL。

数十万〜数百万URLについては、別途設計済みの **Large Site Mode** で対応予定です。Large Site Modeは高速クロールそのものではなく、巨大サイトのSite Structure / Directory / Internal Link Structure解析を中心に設計します。

## セキュリティ

- クロール権限のあるサイトにのみ使用してください。
- 秘密情報をGitHubへコミットしないでください。
- Browser公開時はクラウド側でも内部ネットワークへのegressを制限することを推奨します。
