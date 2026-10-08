from io import BytesIO
import csv
import json
import os
import re
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlsplit

import pandas as pd
import streamlit as st

import crawler
import release_runtime as release_runtime


APP_TITLE = "FRUSTWAY SEO Crawler"

PUBLIC_MODE = (
    os.environ.get(
        "FRUSTWAY_PUBLIC_MODE",
        "1",
    ).strip().lower()
    not in {
        "0",
        "false",
        "no",
        "off",
    }
)

PUBLIC_MAX_PAGES = max(
    1,
    int(
        os.environ.get(
            "FRUSTWAY_PUBLIC_MAX_PAGES",
            "5000",
        )
    ),
)

LOCAL_MAX_PAGES = 50000

JOB_TTL_HOURS = max(
    1,
    int(
        os.environ.get(
            "FRUSTWAY_JOB_TTL_HOURS",
            "24",
        )
    ),
)

JOBS_ROOT = Path(
    os.environ.get(
        "FRUSTWAY_JOBS_ROOT",
        "jobs",
    )
)

DB_PATH = ""
CSV_PATH = ""
META_PATH = ""
JOB_ID = ""
JOB_DIR = None


def load_crawl_meta(meta_path=None):
    path = Path(meta_path or META_PATH)

    if not path.exists():
        return None

    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(data, dict):
            return None

        return data

    except Exception:
        return None


def save_crawl_meta(progress, meta_path=None):
    path = Path(meta_path or META_PATH)
    tmp_path = path.with_suffix(
        ".json.tmp"
    )

    allowed_keys = [
        "running",
        "finished",
        "stopped",
        "stop_requested",
        "event",
        "crawled",
        "max_pages",
        "queued",
        "discovered",
        "current_url",
        "depth",
        "status_code",
        "started_at",
        "finished_at",
        "elapsed",
        "error",
        "start_url",
        "heading_mode",
        "max_depth",
        "polite_mode",
        "include_pattern",
    ]

    payload = {
        key: progress.get(key)
        for key in allowed_keys
    }

    payload["running"] = False
    payload["needs_refresh"] = False

    try:
        tmp_path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        tmp_path.replace(path)

    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def init_crawl_state():
    if "crawl_progress" not in st.session_state:
        saved = load_crawl_meta()

        if saved:
            saved.setdefault(
                "running",
                False,
            )
            saved.setdefault(
                "finished",
                True,
            )
            saved.setdefault(
                "stopped",
                False,
            )
            saved.setdefault(
                "stop_requested",
                False,
            )
            saved.setdefault(
                "event",
                "complete",
            )
            saved.setdefault(
                "crawled",
                0,
            )
            saved.setdefault(
                "max_pages",
                0,
            )
            saved.setdefault(
                "queued",
                0,
            )
            saved.setdefault(
                "discovered",
                0,
            )
            saved.setdefault(
                "current_url",
                "",
            )
            saved.setdefault(
                "depth",
                0,
            )
            saved.setdefault(
                "status_code",
                None,
            )
            saved.setdefault(
                "started_at",
                None,
            )
            saved.setdefault(
                "finished_at",
                None,
            )
            saved.setdefault(
                "elapsed",
                0.0,
            )
            saved.setdefault(
                "error",
                "",
            )
            saved.setdefault(
                "max_depth",
                None,
            )
            saved.setdefault(
                "polite_mode",
                True,
            )
            saved.setdefault(
                "include_pattern",
                "",
            )
            saved["needs_refresh"] = False

            st.session_state[
                "crawl_progress"
            ] = saved

        else:
            st.session_state[
                "crawl_progress"
            ] = {
                "running": False,
                "finished": False,
                "stopped": False,
                "stop_requested": False,
                "event": "",
                "crawled": 0,
                "max_pages": 0,
                "queued": 0,
                "discovered": 0,
                "current_url": "",
                "depth": 0,
                "status_code": None,
                "started_at": None,
                "finished_at": None,
                "elapsed": 0.0,
                "error": "",
                "start_url": "",
                "heading_mode": "basic",
                "max_depth": None,
                "polite_mode": True,
                "include_pattern": "",
                "needs_refresh": False,
            }

    if "crawl_lock" not in st.session_state:
        st.session_state[
            "crawl_lock"
        ] = threading.Lock()

    if "crawl_stop_event" not in st.session_state:
        st.session_state[
            "crawl_stop_event"
        ] = threading.Event()

    if "crawl_thread" not in st.session_state:
        st.session_state[
            "crawl_thread"
        ] = None


def get_crawl_snapshot():
    lock = st.session_state["crawl_lock"]

    with lock:
        return dict(
            st.session_state["crawl_progress"]
        )


st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🔎",
    layout="wide",
)


raw_job_id = release_runtime.normalize_job_id(
    st.query_params.get(
        "job",
        "",
    )
)

session_job_id = release_runtime.normalize_job_id(
    st.session_state.get(
        "active_job_id"
    )
)

if raw_job_id:
    JOB_ID = raw_job_id
elif session_job_id:
    JOB_ID = session_job_id
else:
    JOB_ID = release_runtime.new_job_id()

previous_job = session_job_id

if previous_job != JOB_ID:
    old_stop_event = st.session_state.get(
        "crawl_stop_event"
    )

    if old_stop_event:
        try:
            old_stop_event.set()
        except Exception:
            pass

    for key in [
        "crawl_progress",
        "crawl_lock",
        "crawl_stop_event",
        "crawl_thread",
        "prepared_excel_bytes",
        "prepared_excel_truncated",
        "prepared_spider_export_path",
        "prepared_spider_export_name",
        "prepared_spider_export_rows",
        "page_type_classified",
        "page_type_summary",
        "page_type_transitions",
        "page_type_rule_errors",
    ]:
        st.session_state.pop(
            key,
            None,
        )

st.session_state[
    "active_job_id"
] = JOB_ID

if raw_job_id != JOB_ID:
    st.query_params["job"] = JOB_ID

release_runtime.cleanup_old_jobs(
    JOBS_ROOT,
    ttl_hours=JOB_TTL_HOURS,
    exclude_job_ids={JOB_ID},
)

JOB_DIR = release_runtime.ensure_job_dir(
    JOBS_ROOT,
    JOB_ID,
)

DB_PATH = str(
    JOB_DIR / "seo.db"
)
CSV_PATH = str(
    JOB_DIR / "crawl.csv"
)
META_PATH = str(
    JOB_DIR / "crawl_meta.json"
)

init_crawl_state()

st.title(APP_TITLE)
st.caption("HTML SEO監査用クローラー v1.9.4 Browser Release")

initial_crawl_state = get_crawl_snapshot()
crawl_running = bool(
    initial_crawl_state.get("running")
)


with st.sidebar:
    st.header("Crawl Settings")

    if PUBLIC_MODE:
        st.caption(
            "Browser Beta"
            f" · Max {PUBLIC_MAX_PAGES:,} URLs"
            f" · Jobs retained {JOB_TTL_HOURS}h"
        )
    else:
        st.caption(
            "Local Mode"
            f" · Max {LOCAL_MAX_PAGES:,} URLs"
        )

    st.caption(
        f"Job: {JOB_ID[:8]}"
    )

    if st.button(
        "New Job",
        use_container_width=True,
        disabled=crawl_running,
    ):
        new_job_id = (
            release_runtime.new_job_id()
        )
        st.query_params["job"] = (
            new_job_id
        )
        st.rerun()

    start_url = st.text_input(
        "Start URL",
        value=(
            ""
            if PUBLIC_MODE
            else "https://kamakura-wedding.jp/"
        ),
        placeholder="https://example.com/",
        disabled=crawl_running,
    )

    max_pages_allowed = (
        PUBLIC_MAX_PAGES
        if PUBLIC_MODE
        else LOCAL_MAX_PAGES
    )

    max_pages = st.number_input(
        "Max pages",
        min_value=1,
        max_value=max_pages_allowed,
        value=min(
            1000,
            max_pages_allowed,
        ),
        step=100,
        disabled=crawl_running,
    )

    limit_depth = st.checkbox(
        "Limit crawl depth",
        value=False,
        disabled=crawl_running,
        help=(
            "Depth 0は開始URLです。"
            "3を指定するとDepth 0〜3までクロールし、"
            "Depth 4以降はキューへ追加しません。"
        ),
    )

    max_depth_value = st.number_input(
        "Max crawl depth",
        min_value=0,
        max_value=20,
        value=3,
        step=1,
        disabled=(
            crawl_running
            or not limit_depth
        ),
    )

    max_depth = (
        int(max_depth_value)
        if limit_depth
        else None
    )

    crawl_scope = st.selectbox(
        "Crawl scope",
        [
            "Entire site",
            "Include Regex",
        ],
        index=0,
        disabled=crawl_running,
        help=(
            "Include Regexでは開始URLを入口として1回取得し、"
            "その後は正規表現に一致する内部URLだけをクロールします。"
            "リンク自体は一致・不一致にかかわらず保存します。"
        ),
    )

    include_pattern = ""

    if crawl_scope == "Include Regex":
        include_pattern = st.text_area(
            "Include URL Regex",
            value="",
            placeholder=(
                r"^https://ranking-deli\.jp/notebook/.*"
            ),
            height=90,
            disabled=crawl_running,
            help=(
                "Python正規表現です。"
                "例: ^https://ranking-deli\\.jp/notebook/.*"
            ),
        ).strip()

        st.caption(
            "開始URLはシードとして取得します。"
            "2URL目以降はこのRegexに一致するURLだけをキューへ追加します。"
        )

    polite_mode = st.checkbox(
        "Polite crawl mode",
        value=True,
        disabled=crawl_running,
        help=(
            "対象サーバーへの負荷を抑える推奨設定です。"
            "1クロール内は直列アクセスのまま、"
            "最低1秒の間隔を取り、429/5xx時は自動で待機します。"
        ),
    )

    delay_min = (
        1.0
        if polite_mode
        else (
            0.2
            if PUBLIC_MODE
            else 0.0
        )
    )

    delay_default = (
        1.0
        if polite_mode
        else 0.3
    )

    delay = st.number_input(
        "Delay (seconds)",
        min_value=float(delay_min),
        max_value=10.0,
        value=float(delay_default),
        step=0.1,
        disabled=crawl_running,
    )

    if polite_mode:
        st.caption(
            "負荷軽減: 直列アクセス / 最低1秒間隔 / "
            "429・502・503・504で最大60秒の自動バックオフ"
        )

    heading_label = st.selectbox(
        "Heading extraction",
        [
            "Title + H1 only",
            "Full outline (H1-H6)",
        ],
        index=0,
        help=(
            "通常監査はTitle + H1のみ。"
            "構成監査を行う場合はFull outlineを選択してください。"
        ),
        disabled=crawl_running,
    )

    heading_mode = (
        "full"
        if heading_label == "Full outline (H1-H6)"
        else "basic"
    )

    st.markdown("##### Storage")

    if PUBLIC_MODE:
        store_body_text = False
        store_external_links = False

        st.caption(
            "Browser版では安定性のため、"
            "本文保存・外部リンク保存はOFF固定です。"
        )
    else:
        store_body_text = st.checkbox(
            "Store body text in DB",
            value=False,
            disabled=crawl_running,
            help=(
                "OFF推奨。文字数は取得しますが本文自体は保存しません。"
            ),
        )

        store_external_links = st.checkbox(
            "Store external links",
            value=False,
            disabled=crawl_running,
            help=(
                "OFF推奨。Site Structure / Internal Links解析には"
                "内部リンクだけで十分です。"
            ),
        )

    run_crawl = st.button(
        "Start Crawl",
        type="primary",
        use_container_width=True,
        disabled=crawl_running,
    )


def load_results(csv_path):
    path = Path(csv_path)

    if not path.exists():
        return None

    return pd.read_csv(path)


def db_connect(db_path):
    conn = sqlite3.connect(
        str(db_path),
        timeout=60,
    )
    conn.execute(
        "PRAGMA busy_timeout=60000"
    )
    return conn


def ensure_app_db_schema(db_path):
    """
    旧バージョンのseo_web.dbをV1.8系へ自動移行する。
    parent_url列が無い場合は追加し、既存データについても
    links + depthから可能な範囲でfirst-discovery parentを復元する。
    """
    path = Path(db_path)

    if not path.exists():
        return

    conn = db_connect(path)

    try:
        tables = {
            row[0]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table'
                """
            ).fetchall()
        }

        if "pages" not in tables:
            return

        page_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(pages)"
            ).fetchall()
        }

        added_parent_url = False

        if "parent_url" not in page_columns:
            conn.execute(
                """
                ALTER TABLE pages
                ADD COLUMN parent_url TEXT DEFAULT ''
                """
            )
            added_parent_url = True

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pages_parent
            ON pages(parent_url)
            """
        )

        # 旧クロールデータにはparent_urlが存在しないため、
        # depthが1つ浅いページからの最初の内部リンクを親として復元する。
        if "links" in tables:
            conn.execute(
                """
                UPDATE pages
                SET parent_url = COALESCE(
                    (
                        SELECT l.source_url
                        FROM links l
                        JOIN pages parent
                            ON parent.url = l.source_url
                        WHERE l.is_internal = 1
                          AND l.target_url = pages.url
                          AND parent.depth = pages.depth - 1
                        ORDER BY l.id
                        LIMIT 1
                    ),
                    ''
                )
                WHERE depth > 0
                  AND COALESCE(parent_url, '') = ''
                """
            )

        conn.commit()

    finally:
        conn.close()


def get_internal_link_count(db_path):
    path = Path(db_path)

    if not path.exists():
        return 0

    conn = db_connect(path)

    try:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM links
                WHERE is_internal = 1
                """
            ).fetchone()[0]
        )
    finally:
        conn.close()


def get_page_link_stats(db_path, url):
    conn = db_connect(db_path)

    try:
        row = conn.execute(
            """
            SELECT
                (
                    SELECT COUNT(*)
                    FROM links
                    WHERE is_internal = 1
                      AND source_url = ?
                ) AS outlinks,
                (
                    SELECT COUNT(DISTINCT target_url)
                    FROM links
                    WHERE is_internal = 1
                      AND source_url = ?
                ) AS unique_targets,
                (
                    SELECT COUNT(*)
                    FROM links
                    WHERE is_internal = 1
                      AND target_url = ?
                ) AS inlinks,
                (
                    SELECT COUNT(DISTINCT source_url)
                    FROM links
                    WHERE is_internal = 1
                      AND target_url = ?
                ) AS unique_sources,
                (
                    SELECT COUNT(*)
                    FROM links
                    WHERE is_internal = 1
                      AND source_url = ?
                      AND TRIM(COALESCE(anchor_text, '')) = ''
                ) AS blank_anchor
            """,
            (
                url,
                url,
                url,
                url,
                url,
            ),
        ).fetchone()

        return {
            "outlinks": int(row[0] or 0),
            "unique_targets": int(row[1] or 0),
            "inlinks": int(row[2] or 0),
            "unique_sources": int(row[3] or 0),
            "blank_anchor": int(row[4] or 0),
        }

    finally:
        conn.close()


def load_page_outlinks(db_path, source_url):
    conn = db_connect(db_path)

    try:
        return pd.read_sql_query(
            """
            SELECT
                l.target_url AS "Target URL",
                COALESCE(l.anchor_text, '') AS "Anchor Text",
                p.status_code AS "Target Status",
                p.indexable AS "Target Indexable",
                p.title AS "Target Title",
                p.depth AS "Target Depth",
                COUNT(*) AS "Count"
            FROM links l
            LEFT JOIN pages p
                ON p.url = l.target_url
            WHERE l.is_internal = 1
              AND l.source_url = ?
            GROUP BY
                l.target_url,
                COALESCE(l.anchor_text, ''),
                p.status_code,
                p.indexable,
                p.title,
                p.depth
            ORDER BY
                CASE
                    WHEN p.status_code IS NULL THEN 999
                    ELSE p.status_code
                END,
                l.target_url,
                COALESCE(l.anchor_text, '')
            """,
            conn,
            params=(source_url,),
        )
    finally:
        conn.close()


def load_page_inlinks(db_path, target_url):
    conn = db_connect(db_path)

    try:
        return pd.read_sql_query(
            """
            SELECT
                l.source_url AS "Source URL",
                COALESCE(l.anchor_text, '') AS "Anchor Text",
                p.status_code AS "Source Status",
                p.indexable AS "Source Indexable",
                p.title AS "Source Title",
                p.depth AS "Source Depth",
                COUNT(*) AS "Count"
            FROM links l
            LEFT JOIN pages p
                ON p.url = l.source_url
            WHERE l.is_internal = 1
              AND l.target_url = ?
            GROUP BY
                l.source_url,
                COALESCE(l.anchor_text, ''),
                p.status_code,
                p.indexable,
                p.title,
                p.depth
            ORDER BY
                l.source_url,
                COALESCE(l.anchor_text, '')
            """,
            conn,
            params=(target_url,),
        )
    finally:
        conn.close()


def load_page_anchor_summary(db_path, source_url):
    conn = db_connect(db_path)

    try:
        return pd.read_sql_query(
            """
            SELECT
                CASE
                    WHEN TRIM(COALESCE(anchor_text, '')) = ''
                    THEN '(blank)'
                    ELSE TRIM(anchor_text)
                END AS "Anchor Text",
                COUNT(*) AS "Links",
                COUNT(DISTINCT target_url) AS "Unique Targets"
            FROM links
            WHERE is_internal = 1
              AND source_url = ?
            GROUP BY
                CASE
                    WHEN TRIM(COALESCE(anchor_text, '')) = ''
                    THEN '(blank)'
                    ELSE TRIM(anchor_text)
                END
            ORDER BY
                COUNT(*) DESC,
                COUNT(DISTINCT target_url) DESC
            """,
            conn,
            params=(source_url,),
        )
    finally:
        conn.close()


def load_page_link_issues(db_path, source_url):
    conn = db_connect(db_path)

    try:
        return pd.read_sql_query(
            """
            SELECT
                l.target_url AS "Target URL",
                COALESCE(l.anchor_text, '') AS "Anchor Text",
                p.status_code AS "Target Status",
                p.indexable AS "Target Indexable",
                p.title AS "Target Title",
                p.indexability_reason AS "Target Issues",
                COUNT(*) AS "Count"
            FROM links l
            JOIN pages p
                ON p.url = l.target_url
            WHERE l.is_internal = 1
              AND l.source_url = ?
              AND (
                    p.status_code != 200
                    OR p.indexable = 'No'
              )
            GROUP BY
                l.target_url,
                COALESCE(l.anchor_text, ''),
                p.status_code,
                p.indexable,
                p.title,
                p.indexability_reason
            ORDER BY
                p.status_code,
                l.target_url
            """,
            conn,
            params=(source_url,),
        )
    finally:
        conn.close()


def get_site_link_issue_count(db_path):
    conn = db_connect(db_path)

    try:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM links l
                JOIN pages p
                    ON p.url = l.target_url
                WHERE l.is_internal = 1
                  AND (
                        p.status_code != 200
                        OR p.indexable = 'No'
                  )
                """
            ).fetchone()[0]
        )
    finally:
        conn.close()


def load_site_link_issues(
    db_path,
    limit=1000,
    offset=0,
):
    conn = db_connect(db_path)

    try:
        return pd.read_sql_query(
            """
            SELECT
                l.source_url AS "Source URL",
                l.target_url AS "Target URL",
                COALESCE(l.anchor_text, '') AS "Anchor Text",
                p.status_code AS "Target Status",
                p.indexable AS "Target Indexable",
                p.indexability_reason AS "Target Issues"
            FROM links l
            JOIN pages p
                ON p.url = l.target_url
            WHERE l.is_internal = 1
              AND (
                    p.status_code != 200
                    OR p.indexable = 'No'
              )
            ORDER BY
                p.status_code,
                l.target_url,
                l.id
            LIMIT ?
            OFFSET ?
            """,
            conn,
            params=(
                int(limit),
                int(offset),
            ),
        )
    finally:
        conn.close()


def load_internal_links_for_export(
    db_path,
    max_rows=200000,
):
    conn = db_connect(db_path)

    try:
        return pd.read_sql_query(
            """
            SELECT
                l.source_url AS "Source URL",
                l.target_url AS "Target URL",
                COALESCE(l.anchor_text, '') AS "Anchor Text",
                s.status_code AS "Source Status",
                s.indexable AS "Source Indexable",
                s.title AS "Source Title",
                s.depth AS "Source Depth",
                t.status_code AS "Target Status",
                t.indexable AS "Target Indexable",
                t.title AS "Target Title",
                t.depth AS "Target Depth"
            FROM links l
            LEFT JOIN pages s
                ON s.url = l.source_url
            LEFT JOIN pages t
                ON t.url = l.target_url
            WHERE l.is_internal = 1
            ORDER BY l.id
            LIMIT ?
            """,
            conn,
            params=(int(max_rows),),
        )
    finally:
        conn.close()


SPIDER_EXPORT_SPECS = {
    "Internal > HTML": {
        "filename": "internal_html.csv",
        "sql": """
            SELECT
                url AS "URL",
                status_code AS "Status",
                content_type AS "Content Type",
                indexable AS "Indexable",
                indexability_reason AS "Indexability Reason",
                title AS "Title",
                description AS "Meta Description",
                h1 AS "H1",
                h2 AS "H2",
                depth AS "Crawl Depth",
                canonical AS "Canonical"
            FROM pages
            WHERE LOWER(COALESCE(content_type, ''))
                  LIKE 'text/html%'
            ORDER BY id
        """,
    },
    "All Inlinks": {
        "filename": "all_inlinks.csv",
        "sql": """
            SELECT
                l.target_url AS "Target URL",
                l.source_url AS "Source URL",
                COALESCE(l.anchor_text, '') AS "Anchor Text",
                s.status_code AS "Source Status",
                s.indexable AS "Source Indexable",
                s.depth AS "Source Depth",
                t.status_code AS "Target Status",
                t.indexable AS "Target Indexable",
                t.depth AS "Target Depth"
            FROM links l
            LEFT JOIN pages s ON s.url = l.source_url
            LEFT JOIN pages t ON t.url = l.target_url
            WHERE l.is_internal = 1
            ORDER BY l.target_url, l.source_url, l.id
        """,
    },
    "All Outlinks": {
        "filename": "all_outlinks.csv",
        "sql": """
            SELECT
                l.source_url AS "Source URL",
                l.target_url AS "Target URL",
                COALESCE(l.anchor_text, '') AS "Anchor Text",
                s.status_code AS "Source Status",
                s.indexable AS "Source Indexable",
                s.depth AS "Source Depth",
                t.status_code AS "Target Status",
                t.indexable AS "Target Indexable",
                t.depth AS "Target Depth"
            FROM links l
            LEFT JOIN pages s ON s.url = l.source_url
            LEFT JOIN pages t ON t.url = l.target_url
            WHERE l.is_internal = 1
            ORDER BY l.source_url, l.target_url, l.id
        """,
    },
    "Canonicals": {
        "filename": "canonicals.csv",
        "sql": """
            SELECT
                url AS "URL",
                status_code AS "Status",
                canonical AS "Canonical",
                canonical_match AS "Canonical Match",
                indexable AS "Indexable",
                indexability_reason AS "Indexability Reason"
            FROM pages
            ORDER BY id
        """,
    },
    "Directives": {
        "filename": "directives.csv",
        "sql": """
            SELECT
                url AS "URL",
                status_code AS "Status",
                robots AS "Meta Robots",
                x_robots_tag AS "X-Robots-Tag",
                indexable AS "Indexable",
                indexability_reason AS "Indexability Reason"
            FROM pages
            ORDER BY id
        """,
    },
    "Response Codes": {
        "filename": "response_codes.csv",
        "sql": """
            SELECT
                url AS "URL",
                status_code AS "Status",
                content_type AS "Content Type",
                redirect_url AS "Redirect URL",
                response_time AS "Response Time"
            FROM pages
            ORDER BY id
        """,
    },
    "Page Titles": {
        "filename": "page_titles.csv",
        "sql": """
            SELECT
                url AS "URL",
                status_code AS "Status",
                title AS "Title",
                title_length AS "Title Length",
                indexable AS "Indexable"
            FROM pages
            ORDER BY id
        """,
    },
    "Meta Description": {
        "filename": "meta_description.csv",
        "sql": """
            SELECT
                url AS "URL",
                status_code AS "Status",
                description AS "Meta Description",
                description_length AS "Description Length",
                indexable AS "Indexable"
            FROM pages
            ORDER BY id
        """,
    },
    "H1": {
        "filename": "h1.csv",
        "sql": """
            SELECT
                url AS "URL",
                status_code AS "Status",
                h1 AS "H1",
                h1_count AS "H1 Count",
                indexable AS "Indexable"
            FROM pages
            ORDER BY id
        """,
    },
    "H2": {
        "filename": "h2.csv",
        "sql": """
            SELECT
                url AS "URL",
                status_code AS "Status",
                h2 AS "H2",
                h2_count AS "H2 Count",
                indexable AS "Indexable"
            FROM pages
            ORDER BY id
        """,
    },
    "Crawl Depth": {
        "filename": "crawl_depth.csv",
        "sql": """
            SELECT
                url AS "URL",
                depth AS "Crawl Depth",
                COALESCE(parent_url, '') AS "Parent URL",
                status_code AS "Status",
                indexable AS "Indexable"
            FROM pages
            ORDER BY depth, id
        """,
    },
}


def prepare_spider_export(
    db_path,
    export_name,
    output_path,
):
    spec = SPIDER_EXPORT_SPECS.get(
        export_name
    )

    if not spec:
        raise ValueError(
            "未対応のExportです。"
        )

    conn = db_connect(db_path)
    row_count = 0

    try:
        cursor = conn.execute(
            spec["sql"]
        )

        columns = [
            item[0]
            for item in cursor.description
        ]

        with open(
            output_path,
            "w",
            newline="",
            encoding="utf-8-sig",
        ) as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)

            while True:
                rows = cursor.fetchmany(
                    5000
                )

                if not rows:
                    break

                writer.writerows(rows)
                row_count += len(rows)

    finally:
        conn.close()

    return (
        spec["filename"],
        row_count,
    )


DEFAULT_PAGE_TYPE_RULES = r"""
# First match wins. Format: Page Type => Python Regex
TOP => ^https?://[^/]+/?(?:\\?.*)?$
Notebook => ^https?://[^/]+/notebook(?:/.*)?$
Matome => ^https?://[^/]+/matome(?:/.*)?$
Area x Style => ^https?://[^/]+/[^/]+/area\\d+/style\\d+/?(?:\\?.*)?$
Area => ^https?://[^/]+/[^/]+/area\\d+/?(?:\\?.*)?$
Prefecture x Style => ^https?://[^/]+/[^/]+/style\\d+/?(?:\\?.*)?$
First-level Directory => ^https?://[^/]+/[^/]+/?(?:\\?.*)?$
""".strip()


def parse_page_type_rules(rule_text):
    compiled = []
    errors = []

    for line_no, raw_line in enumerate(
        str(rule_text or "").splitlines(),
        start=1,
    ):
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if "=>" not in line:
            errors.append(
                f"Line {line_no}: 'Page Type => Regex' の形式で入力してください。"
            )
            continue

        label, pattern = line.split(
            "=>",
            1,
        )

        label = label.strip()
        pattern = pattern.strip()

        if not label or not pattern:
            errors.append(
                f"Line {line_no}: Page TypeまたはRegexが空です。"
            )
            continue

        try:
            regex = re.compile(
                pattern
            )
        except re.error as exc:
            errors.append(
                f"Line {line_no} ({label}): {exc}"
            )
            continue

        compiled.append(
            (
                label,
                pattern,
                regex,
            )
        )

    if not compiled and not errors:
        errors.append(
            "Page Typeルールがありません。"
        )

    return compiled, errors


def classify_page_type(
    url,
    compiled_rules,
):
    value = str(url or "")

    for label, pattern, regex in compiled_rules:
        if regex.search(value):
            return label, pattern

    return "Other / Unclassified", ""


def build_page_type_analysis(
    crawl_df,
    db_path,
    compiled_rules,
):
    classified_df = crawl_df.copy()

    classifications = [
        classify_page_type(
            url,
            compiled_rules,
        )
        for url in classified_df[
            "URL"
        ].fillna("")
    ]

    classified_df[
        "Page Type"
    ] = [
        item[0]
        for item in classifications
    ]

    classified_df[
        "Matched Rule"
    ] = [
        item[1]
        for item in classifications
    ]

    for column in (
        "Depth",
        "Unique Internal Inlinks",
        "Unique Internal Outlinks",
    ):
        if column not in classified_df.columns:
            classified_df[column] = 0

        classified_df[
            column
        ] = pd.to_numeric(
            classified_df[column],
            errors="coerce",
        ).fillna(0)

    if "Status" not in classified_df.columns:
        classified_df["Status"] = 0

    classified_df[
        "_status_numeric"
    ] = pd.to_numeric(
        classified_df["Status"],
        errors="coerce",
    ).fillna(0)

    if "Indexable" not in classified_df.columns:
        classified_df["Indexable"] = ""

    grouped = (
        classified_df.groupby(
            "Page Type",
            dropna=False,
        )
        .agg(
            **{
                "URL Count": (
                    "URL",
                    "count",
                ),
                "Avg Depth": (
                    "Depth",
                    "mean",
                ),
                "Avg Unique Inlinks": (
                    "Unique Internal Inlinks",
                    "mean",
                ),
                "Avg Unique Outlinks": (
                    "Unique Internal Outlinks",
                    "mean",
                ),
                "200 OK": (
                    "_status_numeric",
                    lambda x: int(
                        (x == 200).sum()
                    ),
                ),
                "Indexable URLs": (
                    "Indexable",
                    lambda x: int(
                        (
                            x.fillna("")
                            == "Yes"
                        ).sum()
                    ),
                ),
                "Sample URL": (
                    "URL",
                    "first",
                ),
                "URL Pattern": (
                    "Matched Rule",
                    "first",
                ),
            }
        )
        .reset_index()
    )

    grouped[
        "Avg Depth"
    ] = grouped[
        "Avg Depth"
    ].round(2)

    grouped[
        "Avg Unique Inlinks"
    ] = grouped[
        "Avg Unique Inlinks"
    ].round(2)

    grouped[
        "Avg Unique Outlinks"
    ] = grouped[
        "Avg Unique Outlinks"
    ].round(2)

    conn = db_connect(
        db_path
    )

    try:
        conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS page_type_map (
                url TEXT PRIMARY KEY,
                page_type TEXT NOT NULL
            )
            """
        )

        conn.execute(
            "DELETE FROM page_type_map"
        )

        page_type_rows = [
            (
                str(row["URL"]),
                str(row["Page Type"]),
            )
            for _, row in classified_df[
                [
                    "URL",
                    "Page Type",
                ]
            ].iterrows()
        ]

        conn.executemany(
            """
            INSERT OR REPLACE INTO page_type_map (
                url,
                page_type
            )
            VALUES (?, ?)
            """,
            page_type_rows,
        )

        cursor = conn.execute(
            """
            SELECT DISTINCT l.target_url
            FROM links l
            LEFT JOIN page_type_map p
                ON p.url = l.target_url
            WHERE l.is_internal = 1
              AND p.url IS NULL
            """
        )

        while True:
            targets = cursor.fetchmany(
                5000
            )

            if not targets:
                break

            target_rows = []

            for (target_url,) in targets:
                page_type, _ = (
                    classify_page_type(
                        target_url,
                        compiled_rules,
                    )
                )

                target_rows.append(
                    (
                        str(target_url),
                        page_type,
                    )
                )

            conn.executemany(
                """
                INSERT OR IGNORE INTO page_type_map (
                    url,
                    page_type
                )
                VALUES (?, ?)
                """,
                target_rows,
            )

        transitions_df = pd.read_sql_query(
            """
            SELECT
                COALESCE(
                    s.page_type,
                    'Other / Unclassified'
                ) AS "Source Page Type",
                COALESCE(
                    t.page_type,
                    'Other / Unclassified'
                ) AS "Target Page Type",
                COUNT(*) AS "Links",
                COUNT(
                    DISTINCT l.source_url
                ) AS "Unique Source URLs",
                COUNT(
                    DISTINCT l.target_url
                ) AS "Unique Target URLs"
            FROM links l
            LEFT JOIN page_type_map s
                ON s.url = l.source_url
            LEFT JOIN page_type_map t
                ON t.url = l.target_url
            WHERE l.is_internal = 1
            GROUP BY
                COALESCE(
                    s.page_type,
                    'Other / Unclassified'
                ),
                COALESCE(
                    t.page_type,
                    'Other / Unclassified'
                )
            ORDER BY
                COUNT(*) DESC,
                "Source Page Type",
                "Target Page Type"
            """,
            conn,
        )

    finally:
        conn.close()

    if transitions_df.empty:
        grouped[
            "Main Link Sources"
        ] = ""
        grouped[
            "Main Link Targets"
        ] = ""

        return (
            classified_df,
            grouped,
            transitions_df,
        )

    source_lookup = {}
    target_lookup = {}

    for page_type in grouped[
        "Page Type"
    ].astype(str):
        incoming = (
            transitions_df[
                transitions_df[
                    "Target Page Type"
                ].astype(str)
                == page_type
            ]
            .sort_values(
                "Links",
                ascending=False,
            )
            .head(3)
        )

        outgoing = (
            transitions_df[
                transitions_df[
                    "Source Page Type"
                ].astype(str)
                == page_type
            ]
            .sort_values(
                "Links",
                ascending=False,
            )
            .head(3)
        )

        source_lookup[
            page_type
        ] = " | ".join(
            (
                f"{row['Source Page Type']} "
                f"({int(row['Links']):,})"
            )
            for _, row in incoming.iterrows()
        )

        target_lookup[
            page_type
        ] = " | ".join(
            (
                f"{row['Target Page Type']} "
                f"({int(row['Links']):,})"
            )
            for _, row in outgoing.iterrows()
        )

    grouped[
        "Main Link Sources"
    ] = grouped[
        "Page Type"
    ].astype(str).map(
        source_lookup
    ).fillna("")

    grouped[
        "Main Link Targets"
    ] = grouped[
        "Page Type"
    ].astype(str).map(
        target_lookup
    ).fillna("")

    return (
        classified_df,
        grouped,
        transitions_df,
    )


def short_url_label(url, is_root=False):
    """
    Site Structure用表示。
    ルートは scheme://host/、
    配下ページは path + query のみ表示。
    """
    try:
        parts = urlsplit(str(url))

        if is_root:
            return f"{parts.scheme}://{parts.netloc}/"

        path = parts.path or "/"

        if parts.query:
            path += f"?{parts.query}"

        return path

    except Exception:
        return str(url)


def build_site_structure(db_path):
    """
    v1.8ではクローラーがfirst-discovery parentをpages.parent_urlへ保存。
    linksテーブルへN+1問い合わせせず、pagesだけで構造化する。
    """
    path = Path(db_path)

    if not path.exists():
        return pd.DataFrame()

    ensure_app_db_schema(path)
    conn = db_connect(path)

    try:
        page_rows = conn.execute(
            """
            SELECT
                id,
                url,
                depth,
                COALESCE(parent_url, '')
            FROM pages
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    if not page_rows:
        return pd.DataFrame()

    page_info = {
        row[1]: {
            "id": int(row[0]),
            "depth": int(row[2] or 0),
            "parent": row[3] or "",
        }
        for row in page_rows
    }

    children = {
        url: []
        for url in page_info
    }

    roots = []

    for url, info in page_info.items():
        parent = info["parent"]

        if (
            info["depth"] == 0
            or not parent
            or parent not in children
            or parent == url
        ):
            roots.append(url)
        else:
            children[parent].append(url)

    for parent in children:
        children[parent].sort(
            key=lambda u: page_info[u]["id"]
        )

    roots.sort(
        key=lambda u: page_info[u]["id"]
    )

    ordered = []
    visited = set()

    def walk(root):
        stack = [root]

        while stack:
            url = stack.pop()

            if url in visited:
                continue

            visited.add(url)
            ordered.append(url)

            child_list = children.get(
                url,
                [],
            )

            for child in reversed(
                child_list
            ):
                stack.append(child)

    for root in roots:
        walk(root)

    leftovers = [
        url
        for url in page_info
        if url not in visited
    ]

    leftovers.sort(
        key=lambda u: page_info[u]["id"]
    )

    ordered.extend(leftovers)

    max_depth = max(
        info["depth"]
        for info in page_info.values()
    )

    hierarchy_columns = [
        f"Level {i}"
        for i in range(max_depth + 1)
    ]

    rows = []

    for url in ordered:
        info = page_info[url]
        depth = info["depth"]

        row = {
            col: ""
            for col in hierarchy_columns
        }

        row[f"Level {depth}"] = short_url_label(
            url,
            is_root=(depth == 0),
        )

        row["Depth"] = depth
        row["Full URL"] = url
        row["Parent URL"] = info["parent"]

        rows.append(row)

    return pd.DataFrame(rows)


def build_excel_workbook(
    crawl_df,
    structure_df,
    enriched_link_df,
    known_link_issues_df,
):
    """
    Googleスプレッドシートへアップロード可能な.xlsxをメモリ上で作成。
    """
    output = BytesIO()

    issue_df = crawl_df[
        crawl_df["Issues"]
        .fillna("")
        .astype(str)
        .str.strip()
        != ""
    ].copy()

    with pd.ExcelWriter(
        output,
        engine="xlsxwriter",
    ) as writer:
        crawl_df.to_excel(
            writer,
            sheet_name="Crawl Results",
            index=False,
        )

        structure_df.to_excel(
            writer,
            sheet_name="Site Structure",
            index=False,
        )

        enriched_link_df.to_excel(
            writer,
            sheet_name="Internal Links",
            index=False,
        )

        known_link_issues_df.to_excel(
            writer,
            sheet_name="Link Issues",
            index=False,
        )

        issue_df.to_excel(
            writer,
            sheet_name="Issues",
            index=False,
        )

        workbook = writer.book

        header_format = workbook.add_format(
            {
                "bold": True,
                "bg_color": "#1F4E78",
                "font_color": "#FFFFFF",
                "border": 1,
                "valign": "vcenter",
            }
        )

        wrap_format = workbook.add_format(
            {
                "text_wrap": True,
                "valign": "top",
            }
        )

        link_format = workbook.add_format(
            {
                "font_color": "#0563C1",
                "underline": 1,
                "valign": "top",
            }
        )

        # Crawl Results
        ws = writer.sheets["Crawl Results"]
        ws.freeze_panes(1, 1)
        ws.autofilter(
            0,
            0,
            len(crawl_df),
            max(len(crawl_df.columns) - 1, 0),
        )

        for col_num, column in enumerate(crawl_df.columns):
            ws.write(
                0,
                col_num,
                column,
                header_format,
            )

            width = 18

            if column in (
                "URL",
                "Canonical",
                "Redirect URL",
            ):
                width = 50

            elif column in (
                "Title",
                "Description",
                "H1",
                "H2",
                "Headings Outline",
                "Issues",
            ):
                width = 40

            ws.set_column(
                col_num,
                col_num,
                width,
                wrap_format,
            )

        # Site Structure
        ws_structure = writer.sheets["Site Structure"]
        ws_structure.freeze_panes(1, 0)

        for col_num, column in enumerate(structure_df.columns):
            ws_structure.write(
                0,
                col_num,
                column,
                header_format,
            )

            if column.startswith("Level "):
                ws_structure.set_column(
                    col_num,
                    col_num,
                    42,
                    wrap_format,
                )
            elif column in ("Full URL", "Parent URL"):
                ws_structure.set_column(
                    col_num,
                    col_num,
                    55,
                    link_format,
                )
            else:
                ws_structure.set_column(
                    col_num,
                    col_num,
                    12,
                )

        # 階層を目で追いやすくする
        level_cols = [
            col
            for col in structure_df.columns
            if col.startswith("Level ")
        ]

        level_formats = []

        level_colors = [
            "#D9EAF7",
            "#E2F0D9",
            "#FFF2CC",
            "#FCE4D6",
            "#E4DFEC",
            "#DDEBF7",
            "#EDEDED",
        ]

        for i, _ in enumerate(level_cols):
            level_formats.append(
                workbook.add_format(
                    {
                        "bg_color": level_colors[
                            i % len(level_colors)
                        ],
                        "text_wrap": True,
                        "valign": "top",
                        "border": 1,
                    }
                )
            )

        for row_num, (_, row) in enumerate(
            structure_df.iterrows(),
            start=1,
        ):
            for level_index, col in enumerate(level_cols):
                value = row.get(col, "")

                if pd.isna(value):
                    value = ""

                ws_structure.write(
                    row_num,
                    level_index,
                    value,
                    level_formats[level_index],
                )

        # Internal Links
        ws_links = writer.sheets["Internal Links"]
        ws_links.freeze_panes(1, 0)
        ws_links.autofilter(
            0,
            0,
            len(enriched_link_df),
            max(len(enriched_link_df.columns) - 1, 0),
        )

        for col_num, column in enumerate(enriched_link_df.columns):
            ws_links.write(
                0,
                col_num,
                column,
                header_format,
            )

            width = 45 if "URL" in column else 35

            ws_links.set_column(
                col_num,
                col_num,
                width,
                wrap_format,
            )

        # Link Issues
        ws_link_issues = writer.sheets["Link Issues"]
        ws_link_issues.freeze_panes(1, 0)

        if len(known_link_issues_df.columns) > 0:
            ws_link_issues.autofilter(
                0,
                0,
                len(known_link_issues_df),
                len(known_link_issues_df.columns) - 1,
            )

        for col_num, column in enumerate(known_link_issues_df.columns):
            ws_link_issues.write(
                0,
                col_num,
                column,
                header_format,
            )

            width = 45 if "URL" in column else 28

            if "Title" in column or "Issues" in column:
                width = 38

            ws_link_issues.set_column(
                col_num,
                col_num,
                width,
                wrap_format,
            )

        # Issues
        ws_issues = writer.sheets["Issues"]
        ws_issues.freeze_panes(1, 1)

        for col_num, column in enumerate(issue_df.columns):
            ws_issues.write(
                0,
                col_num,
                column,
                header_format,
            )

            width = 18

            if column in (
                "URL",
                "Canonical",
            ):
                width = 50

            elif column in (
                "Title",
                "H1",
                "Issues",
            ):
                width = 40

            ws_issues.set_column(
                col_num,
                col_num,
                width,
                wrap_format,
            )

    output.seek(0)
    return output.getvalue()


def start_crawl_job(
    start_url,
    max_pages,
    delay,
    heading_mode,
    store_body_text,
    store_external_links,
    public_mode,
    max_depth,
    polite_mode,
    include_pattern,
):
    slot_acquired = False

    if public_mode:
        slot_acquired = (
            release_runtime.acquire_crawl_slot()
        )

        if not slot_acquired:
            return False

    stop_event = threading.Event()
    lock = threading.Lock()

    st.session_state["crawl_stop_event"] = stop_event
    st.session_state["crawl_lock"] = lock

    progress = {
        "running": True,
        "finished": False,
        "stopped": False,
        "stop_requested": False,
        "event": "starting",
        "crawled": 0,
        "max_pages": int(max_pages),
        "queued": 0,
        "discovered": 1,
        "current_url": start_url,
        "depth": 0,
        "status_code": None,
        "started_at": time.time(),
        "finished_at": None,
        "elapsed": 0.0,
        "error": "",
        "start_url": start_url,
        "heading_mode": heading_mode,
        "max_depth": max_depth,
        "polite_mode": bool(polite_mode),
        "include_pattern": str(include_pattern or ""),
        "needs_refresh": False,
    }

    st.session_state["crawl_progress"] = progress

    for key in [
        "prepared_excel_bytes",
        "prepared_excel_truncated",
        "prepared_spider_export_path",
        "prepared_spider_export_name",
        "prepared_spider_export_rows",
        "page_type_classified",
        "page_type_summary",
        "page_type_transitions",
        "page_type_rule_errors",
    ]:
        st.session_state.pop(
            key,
            None,
        )

    # 新しいクロール開始時は前回の完了メタ情報を消す。
    meta_path = Path(META_PATH)

    if meta_path.exists():
        try:
            meta_path.unlink()
        except OSError:
            pass

    db_path = str(DB_PATH)
    csv_path_value = str(CSV_PATH)
    meta_path_value = str(META_PATH)

    csv_path = Path(
        csv_path_value
    )

    if csv_path.exists():
        try:
            csv_path.unlink()
        except OSError:
            pass

    def worker():
        def update_progress(payload):
            now = time.time()

            with lock:
                progress.update(payload)
                progress["elapsed"] = (
                    now - progress["started_at"]
                    if progress["started_at"]
                    else 0.0
                )

        try:
            conn = crawler.crawl(
                start_url,
                int(max_pages),
                float(delay),
                db_path,
                heading_mode=heading_mode,
                progress_callback=update_progress,
                should_stop=stop_event.is_set,
                store_body_text=store_body_text,
                store_external_links=store_external_links,
                commit_every=25,
                public_mode=public_mode,
                max_depth=max_depth,
                adaptive_backoff=True,
                include_pattern=include_pattern,
            )

            crawler.export_csv(
                conn,
                csv_path_value,
            )

            conn.close()

            with lock:
                progress["running"] = False
                progress["finished"] = True
                progress["stopped"] = (
                    progress.get("event")
                    == "stopped"
                    or stop_event.is_set()
                )
                progress["finished_at"] = time.time()
                progress["elapsed"] = (
                    progress["finished_at"]
                    - progress["started_at"]
                )
                progress["needs_refresh"] = True

                completed_snapshot = dict(
                    progress
                )

            save_crawl_meta(
                completed_snapshot,
                meta_path_value,
            )

        except Exception as exc:
            with lock:
                progress["running"] = False
                progress["finished"] = True
                progress["finished_at"] = time.time()
                progress["elapsed"] = (
                    progress["finished_at"]
                    - progress["started_at"]
                )
                progress["error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                progress["needs_refresh"] = True

                failed_snapshot = dict(
                    progress
                )

            save_crawl_meta(
                failed_snapshot,
                meta_path_value,
            )

        finally:
            release_runtime.touch_job(
                JOB_DIR
            )

            if slot_acquired:
                release_runtime.release_crawl_slot()

    thread = threading.Thread(
        target=worker,
        name="frustway-crawler",
        daemon=True,
    )

    st.session_state["crawl_thread"] = thread

    try:
        thread.start()
    except Exception:
        if slot_acquired:
            release_runtime.release_crawl_slot()
        raise

    return True


def request_crawl_stop():
    stop_event = st.session_state.get(
        "crawl_stop_event"
    )

    if stop_event:
        stop_event.set()

    lock = st.session_state["crawl_lock"]

    with lock:
        st.session_state[
            "crawl_progress"
        ]["stop_requested"] = True


def format_elapsed(seconds):
    seconds = max(
        0,
        int(seconds or 0),
    )

    minutes, seconds = divmod(
        seconds,
        60,
    )

    hours, minutes = divmod(
        minutes,
        60,
    )

    if hours:
        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

    return f"{minutes:02d}:{seconds:02d}"


def render_crawl_progress():
    state = get_crawl_snapshot()

    # 完了・停止・上限到達後はResults画面へ移るため、
    # 進捗パネルはクロール中だけ表示する。
    if not state.get("running"):
        return

    st.subheader("Crawl Progress")

    crawled = int(
        state.get("crawled") or 0
    )
    max_pages_value = max(
        int(
            state.get("max_pages") or 1
        ),
        1,
    )

    progress_ratio = min(
        crawled / max_pages_value,
        1.0,
    )

    if (
        state.get("finished")
        and not state.get("error")
    ):
        progress_ratio = 1.0

    st.progress(
        progress_ratio,
        text=(
            f"{crawled:,} / "
            f"{max_pages_value:,} pages"
        ),
    )

    metric_cols = st.columns(6)

    metric_cols[0].metric(
        "Crawled",
        f"{crawled:,}",
    )
    metric_cols[1].metric(
        "Discovered",
        f"{int(state.get('discovered') or 0):,}",
    )
    metric_cols[2].metric(
        "Queue",
        f"{int(state.get('queued') or 0):,}",
    )
    metric_cols[3].metric(
        "Depth",
        int(state.get("depth") or 0),
    )
    metric_cols[4].metric(
        "Elapsed",
        format_elapsed(
            state.get("elapsed")
        ),
    )

    elapsed = float(
        state.get("elapsed") or 0
    )

    pages_per_minute = (
        (crawled / elapsed) * 60
        if elapsed > 0
        else 0
    )

    metric_cols[5].metric(
        "Pages / min",
        f"{pages_per_minute:,.1f}",
    )

    current_url = (
        state.get("current_url")
        or ""
    )

    if current_url:
        st.caption(
            "Current URL"
        )
        st.code(
            current_url,
            language=None,
        )

    if state.get("stop_requested"):
        st.warning(
            "停止要求を受け付けました。"
            "現在のHTTPリクエストが終了した時点で停止します。"
        )
    else:
        if st.button(
            "Stop Crawl",
            type="secondary",
            use_container_width=True,
            key="stop_crawl_button",
        ):
            request_crawl_stop()
            st.rerun(scope="fragment")


if run_crawl:
    if not start_url.strip():
        st.error("Start URLを入力してください。")
        st.stop()

    if not (
        start_url.startswith("http://")
        or start_url.startswith("https://")
    ):
        st.error("URLは http:// または https:// から入力してください。")
        st.stop()

    if PUBLIC_MODE:
        try:
            crawler.validate_public_url(
                start_url.strip()
            )
        except Exception as exc:
            st.error(
                "このURLはBrowser版ではクロールできません。"
                f" {exc}"
            )
            st.stop()

    if crawl_scope == "Include Regex":
        if not include_pattern:
            st.error(
                "Include URL Regexを入力してください。"
            )
            st.stop()

        try:
            re.compile(
                include_pattern
            )
        except re.error as exc:
            st.error(
                f"Include Regexが不正です: {exc}"
            )
            st.stop()

    started = start_crawl_job(
        start_url.strip(),
        int(max_pages),
        float(delay),
        heading_mode,
        store_body_text,
        store_external_links,
        PUBLIC_MODE,
        max_depth,
        polite_mode,
        include_pattern,
    )

    if not started:
        st.error(
            "現在クロール処理が混雑しています。"
            f" 同時実行上限は "
            f"{release_runtime.max_concurrent_crawls()} 件です。"
            " 少し待ってから再実行してください。"
        )
        st.stop()

    st.rerun()


refresh_interval = (
    0.5
    if get_crawl_snapshot().get(
        "running"
    )
    else None
)


@st.fragment(
    run_every=refresh_interval
)
def crawl_progress_fragment():
    render_crawl_progress()

    state = get_crawl_snapshot()

    if (
        not state.get("running")
        and state.get("needs_refresh")
    ):
        lock = st.session_state[
            "crawl_lock"
        ]

        with lock:
            st.session_state[
                "crawl_progress"
            ]["needs_refresh"] = False

        # 進捗Fragmentだけでなくアプリ全体を再実行し、
        # 自動的にResults画面へ遷移させる。
        st.rerun(scope="app")


crawl_progress_fragment()


if get_crawl_snapshot().get("running"):
    st.stop()


release_runtime.touch_job(
    JOB_DIR
)

df = load_results(CSV_PATH)

# V1.7以前の既存DBもそのまま開けるよう、
# Results表示前に必要なスキーマを自動移行する。
ensure_app_db_schema(DB_PATH)

if df is None or df.empty:
    st.info(
        "左側でURLと最大ページ数を指定して、"
        "「Start Crawl」を押してください。"
    )

    if PUBLIC_MODE:
        st.caption(
            "このJobのURLを再読み込みすれば、"
            f"最大{JOB_TTL_HOURS}時間は同じJobを復元できます。"
        )

    st.stop()


if PUBLIC_MODE:
    st.caption(
        f"Job {JOB_ID[:8]} · "
        f"データ保持目安 {JOB_TTL_HOURS}時間"
    )


final_state = get_crawl_snapshot()
final_event = final_state.get("event", "")

result_max_depth = final_state.get(
    "max_depth"
)

if result_max_depth is not None:
    st.info(
        "Depth制限を使用したクロールです。"
        f" Depth 0〜{int(result_max_depth)}まで取得し、"
        f"Depth {int(result_max_depth) + 1}以降はクロールしていません。"
        " 上限階層ページ上の内部リンク自体はリンクデータとして保存しています。"
    )

result_include_pattern = str(
    final_state.get(
        "include_pattern"
    )
    or ""
).strip()

if result_include_pattern:
    st.info(
        "Include Regexを使用したクロールです。"
        " 開始URL以外は次のRegexに一致するURLだけをクロールしました。"
    )
    st.code(
        result_include_pattern,
        language=None,
    )

final_crawled = int(
    final_state.get("crawled") or len(df)
)
final_discovered = int(
    final_state.get("discovered") or final_crawled
)
final_queued = int(
    final_state.get("queued") or 0
)

if final_state.get("error"):
    st.error(
        "クロール中にエラーが発生しました。"
        f"取得済みの {len(df):,} ページ分のみ表示しています。"
    )

elif final_event == "limit_reached":
    remaining_candidates = max(
        final_discovered - final_crawled,
        final_queued,
        0,
    )

    st.warning(
        "クロール上限に達しました。"
        f"現在 {final_crawled:,} ページを取得済みで、"
        f"未クロール候補が少なくとも {remaining_candidates:,} URL残っています。"
        "現状取得できたデータのみ表示しています。"
        "必要であれば Max pages を増やして再クロールしてください。"
    )

elif final_event == "stopped" or final_state.get("stopped"):
    st.warning(
        "クロールを途中で停止しました。"
        f"取得済みの {final_crawled:,} ページ分のみ表示しています。"
    )

elif final_event == "complete" and final_state.get("finished"):
    st.success(
        f"クロール完了：{final_crawled:,}ページ"
    )


structure_df = build_site_structure(DB_PATH)
internal_link_count = get_internal_link_count(
    DB_PATH
)
site_link_issue_count = get_site_link_issue_count(
    DB_PATH
)


status_numeric = pd.to_numeric(
    df["Status"],
    errors="coerce",
).fillna(0)

pages_crawled = len(df)
ok_200 = int((status_numeric == 200).sum())
redirects = int(
    ((status_numeric >= 300) & (status_numeric <= 399)).sum()
)
errors_404 = int((status_numeric == 404).sum())
errors_5xx = int((status_numeric >= 500).sum())
indexable_count = int(
    (df["Indexable"].fillna("") == "Yes").sum()
)

metric_cols = st.columns(6)

metric_cols[0].metric("Pages", pages_crawled)
metric_cols[1].metric("200 OK", ok_200)
metric_cols[2].metric("3xx", redirects)
metric_cols[3].metric("404", errors_404)
metric_cols[4].metric("5xx", errors_5xx)
metric_cols[5].metric("Indexable", indexable_count)


st.subheader("Results")

filter_cols = st.columns([2, 1, 1, 1])

search_text = filter_cols[0].text_input(
    "URL / Title search",
    value="",
)

status_filter = filter_cols[1].selectbox(
    "Status",
    [
        "All",
        "200",
        "3xx",
        "4xx",
        "5xx",
    ],
)

indexable_filter = filter_cols[2].selectbox(
    "Indexable",
    [
        "All",
        "Yes",
        "No",
    ],
)

issues_only = filter_cols[3].checkbox(
    "Issues only",
    value=False,
)

filtered = df.copy()

if search_text:
    needle = search_text.lower()

    url_series = filtered["URL"].fillna("").astype(str).str.lower()
    title_series = filtered["Title"].fillna("").astype(str).str.lower()

    filtered = filtered[
        url_series.str.contains(
            needle,
            regex=False,
        )
        | title_series.str.contains(
            needle,
            regex=False,
        )
    ]

status_series = pd.to_numeric(
    filtered["Status"],
    errors="coerce",
).fillna(0)

if status_filter == "200":
    filtered = filtered[status_series == 200]

elif status_filter == "3xx":
    filtered = filtered[
        (status_series >= 300)
        & (status_series <= 399)
    ]

elif status_filter == "4xx":
    filtered = filtered[
        (status_series >= 400)
        & (status_series <= 499)
    ]

elif status_filter == "5xx":
    filtered = filtered[
        status_series >= 500
    ]

if indexable_filter != "All":
    filtered = filtered[
        filtered["Indexable"].fillna("")
        == indexable_filter
    ]

if issues_only:
    filtered = filtered[
        filtered["Issues"]
        .fillna("")
        .astype(str)
        .str.strip()
        != ""
    ]


tabs = st.tabs(
    [
        "Internal",
        "Internal Links",
        "Response Codes",
        "Page Titles",
        "Headings",
        "Site Structure",
        "Page Types",
        "Canonicals",
        "Issues",
    ]
)

(
    tab_internal,
    tab_links,
    tab_status,
    tab_titles,
    tab_headings,
    tab_structure,
    tab_page_types,
    tab_canonical,
    tab_issues,
) = tabs


with tab_internal:
    cols = [
        "URL",
        "Status",
        "Indexable",
        "Title",
        "Title Length",
        "H1",
        "H1 Count",
        "Depth",
        "Response Time",
        "Unique Internal Inlinks",
        "Unique Internal Outlinks",
        "Issues",
    ]

    cols = [c for c in cols if c in filtered.columns]

    st.dataframe(
        filtered[cols],
        use_container_width=True,
        hide_index=True,
    )


with tab_links:
    st.caption(
        "内部リンク全件をRAMへ読み込まず、選択したページ分だけSQLiteから取得します。"
    )

    if internal_link_count == 0:
        st.info("内部リンクデータがありません。")
    else:
        page_options = (
            df["URL"]
            .dropna()
            .astype(str)
            .drop_duplicates()
            .tolist()
        )

        selected_page = st.selectbox(
            "確認するページ",
            page_options,
            key="internal_link_page",
        )

        stats = get_page_link_stats(
            DB_PATH,
            selected_page,
        )

        page_outlinks = load_page_outlinks(
            DB_PATH,
            selected_page,
        )
        page_inlinks = load_page_inlinks(
            DB_PATH,
            selected_page,
        )
        anchor_summary = load_page_anchor_summary(
            DB_PATH,
            selected_page,
        )
        known_outlink_issues = load_page_link_issues(
            DB_PATH,
            selected_page,
        )

        uncrawled_targets = 0

        if (
            not page_outlinks.empty
            and "Target Status"
            in page_outlinks.columns
        ):
            uncrawled_targets = int(
                pd.to_numeric(
                    page_outlinks[
                        "Target Status"
                    ],
                    errors="coerce",
                )
                .isna()
                .sum()
            )

        metric_cols = st.columns(6)

        metric_cols[0].metric(
            "Outlinks",
            f"{stats['outlinks']:,}",
        )
        metric_cols[1].metric(
            "Unique Targets",
            f"{stats['unique_targets']:,}",
        )
        metric_cols[2].metric(
            "Inlinks",
            f"{stats['inlinks']:,}",
        )
        metric_cols[3].metric(
            "Unique Sources",
            f"{stats['unique_sources']:,}",
        )
        metric_cols[4].metric(
            "Blank Anchor",
            f"{stats['blank_anchor']:,}",
        )
        metric_cols[5].metric(
            "Known Link Issues",
            f"{len(known_outlink_issues):,}",
        )

        sub_out, sub_in, sub_anchor, sub_issues = st.tabs(
            [
                "Outlinks",
                "Inlinks",
                "Anchor Summary",
                "Link Issues",
            ]
        )

        with sub_out:
            st.caption(
                "このページから貼られている内部リンク。"
                "同一リンク先×同一アンカーはCountで集約します。"
            )

            if page_outlinks.empty:
                st.info(
                    "このページからの内部リンクはありません。"
                )
            else:
                st.dataframe(
                    page_outlinks,
                    use_container_width=True,
                    hide_index=True,
                )

                if uncrawled_targets:
                    st.caption(
                        f"※ Target未クロール: {uncrawled_targets:,}件。"
                        "Max pages上限外などのURLは問題判定していません。"
                    )

        with sub_in:
            st.caption(
                "このページへ内部リンクしているページとアンカーテキスト。"
            )

            if page_inlinks.empty:
                st.info(
                    "このページへの内部リンクはありません。"
                )
            else:
                st.dataframe(
                    page_inlinks,
                    use_container_width=True,
                    hide_index=True,
                )

        with sub_anchor:
            st.caption(
                "このページから使っているアンカーテキスト集計。"
            )

            if anchor_summary.empty:
                st.info(
                    "集計対象の内部リンクがありません。"
                )
            else:
                st.dataframe(
                    anchor_summary,
                    use_container_width=True,
                    hide_index=True,
                )

        with sub_issues:
            st.caption(
                "クロール済みリンク先のうち、"
                "200以外またはIndexable=Noの内部リンク。"
            )

            if known_outlink_issues.empty:
                st.success(
                    "このページでは既知の内部リンク問題は見つかりませんでした。"
                )
            else:
                st.dataframe(
                    known_outlink_issues,
                    use_container_width=True,
                    hide_index=True,
                )

        st.markdown(
            "#### Site-wide Internal Link Issues"
        )

        st.caption(
            f"既知の問題リンク総数: {site_link_issue_count:,}"
        )

        if site_link_issue_count == 0:
            st.success(
                "クロール範囲内では既知の内部リンク問題はありません。"
            )
        else:
            issue_page_size = 500
            issue_pages = max(
                (
                    site_link_issue_count
                    + issue_page_size
                    - 1
                )
                // issue_page_size,
                1,
            )

            issue_page = st.number_input(
                "Issue page",
                min_value=1,
                max_value=issue_pages,
                value=1,
                step=1,
                key="link_issue_page",
            )

            site_issue_df = (
                load_site_link_issues(
                    DB_PATH,
                    limit=issue_page_size,
                    offset=(
                        int(issue_page) - 1
                    )
                    * issue_page_size,
                )
            )

            st.dataframe(
                site_issue_df,
                use_container_width=True,
                hide_index=True,
            )

            st.caption(
                f"{int(issue_page):,} / {issue_pages:,} ページ "
                f"（1ページ {issue_page_size:,}件）"
            )


with tab_status:
    cols = [
        "URL",
        "Status",
        "Redirect URL",
        "Indexable",
        "Indexability Reason",
        "Response Time",
        "Issues",
    ]

    cols = [c for c in cols if c in filtered.columns]

    st.dataframe(
        filtered[cols],
        use_container_width=True,
        hide_index=True,
    )


with tab_titles:
    cols = [
        "URL",
        "Status",
        "Title",
        "Title Length",
        "Indexable",
        "Issues",
    ]

    cols = [c for c in cols if c in filtered.columns]

    st.dataframe(
        filtered[cols],
        use_container_width=True,
        hide_index=True,
    )


with tab_headings:
    current_mode = ""

    if "Heading Mode" in filtered.columns and not filtered.empty:
        modes = (
            filtered["Heading Mode"]
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        )

        if modes:
            current_mode = modes[0]

    if current_mode != "full":
        st.info(
            "このクロールは「Title + H1 only」モードです。"
            "H2以下を確認する場合は、左側のHeading extractionを"
            "「Full outline (H1-H6)」にして再クロールしてください。"
        )

        cols = [
            "URL",
            "Title",
            "H1",
            "H1 Count",
            "Issues",
        ]

        cols = [c for c in cols if c in filtered.columns]

        st.dataframe(
            filtered[cols],
            use_container_width=True,
            hide_index=True,
        )

    else:
        st.caption(
            "H1〜H6をHTML上の出現順で取得しています。"
        )

        cols = [
            "URL",
            "Title",
            "H1 Count",
            "H2 Count",
            "H3 Count",
            "H4 Count",
            "H5 Count",
            "H6 Count",
            "Issues",
        ]

        cols = [c for c in cols if c in filtered.columns]

        st.dataframe(
            filtered[cols],
            use_container_width=True,
            hide_index=True,
        )

        if not filtered.empty:
            url_options = filtered["URL"].astype(str).tolist()

            selected_url = st.selectbox(
                "見出し構成を確認するページ",
                url_options,
            )

            selected = filtered[
                filtered["URL"].astype(str) == selected_url
            ].iloc[0]

            st.markdown(
                f"**Title:** {selected.get('Title', '')}"
            )

            outline = selected.get(
                "Headings Outline",
                "",
            )

            if pd.isna(outline) or not str(outline).strip():
                st.warning("見出しが取得できませんでした。")
            else:
                st.code(
                    str(outline),
                    language=None,
                )


with tab_structure:
    st.caption(
        "ルートドメインをLevel 0として、"
        "クロール深度が1つ下がるごとに右の列へ配置しています。"
        "親子関係は実際の内部リンクとクロール順から構築しています。"
    )

    if structure_df.empty:
        st.info("サイト構造データがありません。")
    else:
        hierarchy_cols = [
            col
            for col in structure_df.columns
            if col.startswith("Level ")
        ]

        st.dataframe(
            structure_df[hierarchy_cols],
            use_container_width=True,
            hide_index=True,
            height=600,
        )


with tab_page_types:
    st.caption(
        "URLパターンからページタイプを分類し、"
        "URL数・平均Depth・平均Inlinks/Outlinks・"
        "主なリンク元/リンク先を自動集計します。"
    )

    rule_text = st.text_area(
        "Page Type Rules",
        value=DEFAULT_PAGE_TYPE_RULES,
        height=250,
        key="page_type_rules",
        help=(
            "上から順に最初に一致したルールを採用します。"
            "形式: Page Type => Python Regex"
        ),
    )

    st.caption(
        "デフォルトは汎用ルールです。"
        "店舗詳細・女の子詳細・ホテル等は、"
        "対象サイトのURLパターンが分かれば上に追記してください。"
    )

    if st.button(
        "Analyze Page Types",
        type="primary",
        use_container_width=True,
        key="analyze_page_types",
    ):
        compiled_rules, rule_errors = (
            parse_page_type_rules(
                rule_text
            )
        )

        st.session_state[
            "page_type_rule_errors"
        ] = rule_errors

        if not rule_errors:
            with st.spinner(
                "ページタイプと内部リンク構造を集計しています..."
            ):
                (
                    classified_pages,
                    page_type_summary,
                    page_type_transitions,
                ) = build_page_type_analysis(
                    df,
                    DB_PATH,
                    compiled_rules,
                )

            st.session_state[
                "page_type_classified"
            ] = classified_pages
            st.session_state[
                "page_type_summary"
            ] = page_type_summary
            st.session_state[
                "page_type_transitions"
            ] = page_type_transitions

    rule_errors = st.session_state.get(
        "page_type_rule_errors",
        [],
    )

    if rule_errors:
        for error in rule_errors:
            st.error(
                error
            )

    page_type_summary = st.session_state.get(
        "page_type_summary"
    )
    page_type_transitions = st.session_state.get(
        "page_type_transitions"
    )
    classified_pages = st.session_state.get(
        "page_type_classified"
    )

    if (
        isinstance(
            page_type_summary,
            pd.DataFrame,
        )
        and not page_type_summary.empty
    ):
        st.markdown(
            "#### Page Type Summary"
        )

        st.dataframe(
            page_type_summary,
            use_container_width=True,
            hide_index=True,
        )

        summary_cols = st.columns(2)

        summary_cols[
            0
        ].download_button(
            "Download Page Type Summary CSV",
            data=(
                page_type_summary
                .to_csv(
                    index=False
                )
                .encode(
                    "utf-8-sig"
                )
            ),
            file_name=(
                "page_type_summary.csv"
            ),
            mime="text/csv",
            use_container_width=True,
            key="download_page_type_summary",
        )

        if (
            isinstance(
                page_type_transitions,
                pd.DataFrame,
            )
            and not page_type_transitions.empty
        ):
            summary_cols[
                1
            ].download_button(
                "Download Link Map CSV",
                data=(
                    page_type_transitions
                    .to_csv(
                        index=False
                    )
                    .encode(
                        "utf-8-sig"
                    )
                ),
                file_name=(
                    "page_type_link_map.csv"
                ),
                mime="text/csv",
                use_container_width=True,
                key="download_page_type_link_map",
            )

            st.markdown(
                "#### Page Type Link Map"
            )

            st.caption(
                "Source Page Type → Target Page Type の"
                "内部リンク本数と、リンク元/リンク先URL数です。"
            )

            st.dataframe(
                page_type_transitions,
                use_container_width=True,
                hide_index=True,
            )

        if (
            isinstance(
                classified_pages,
                pd.DataFrame,
            )
            and not classified_pages.empty
        ):
            st.markdown(
                "#### Classified URLs"
            )

            classified_cols = [
                "Page Type",
                "URL",
                "Depth",
                "Unique Internal Inlinks",
                "Unique Internal Outlinks",
                "Status",
                "Indexable",
                "Title",
                "Matched Rule",
            ]

            classified_cols = [
                col
                for col in classified_cols
                if col in classified_pages.columns
            ]

            st.dataframe(
                classified_pages[
                    classified_cols
                ],
                use_container_width=True,
                hide_index=True,
                height=500,
            )

            st.download_button(
                "Download Classified URLs CSV",
                data=(
                    classified_pages[
                        classified_cols
                    ]
                    .to_csv(
                        index=False
                    )
                    .encode(
                        "utf-8-sig"
                    )
                ),
                file_name=(
                    "classified_urls.csv"
                ),
                mime="text/csv",
                use_container_width=True,
                key="download_classified_urls",
            )


with tab_canonical:
    cols = [
        "URL",
        "Status",
        "Canonical",
        "Canonical Match",
        "Indexable",
        "Indexability Reason",
        "Issues",
    ]

    cols = [c for c in cols if c in filtered.columns]

    st.dataframe(
        filtered[cols],
        use_container_width=True,
        hide_index=True,
    )


with tab_issues:
    issue_df = filtered[
        filtered["Issues"]
        .fillna("")
        .astype(str)
        .str.strip()
        != ""
    ]

    cols = [
        "URL",
        "Status",
        "Indexable",
        "Issues",
        "Title",
        "H1",
        "Canonical",
    ]

    cols = [c for c in cols if c in issue_df.columns]

    st.dataframe(
        issue_df[cols],
        use_container_width=True,
        hide_index=True,
    )


st.divider()
st.subheader("Export")

export_cols = st.columns(2)

csv_path = Path(CSV_PATH)

if csv_path.exists():
    export_cols[0].download_button(
        "Download CSV",
        data=csv_path.read_bytes(),
        file_name="frustway_seo_crawl.csv",
        mime="text/csv",
        use_container_width=True,
    )

with export_cols[1]:
    st.caption(
        f"Internal Links stored: {internal_link_count:,}"
    )

    if st.button(
        "Prepare Excel (.xlsx)",
        use_container_width=True,
        key="prepare_excel",
    ):
        with st.spinner(
            "Excelを生成しています..."
        ):
            max_excel_links = 200000

            export_links = (
                load_internal_links_for_export(
                    DB_PATH,
                    max_rows=max_excel_links,
                )
            )

            export_link_issues = (
                load_site_link_issues(
                    DB_PATH,
                    limit=100000,
                    offset=0,
                )
            )

            excel_bytes = build_excel_workbook(
                df,
                structure_df,
                export_links,
                export_link_issues,
            )

            st.session_state[
                "prepared_excel_bytes"
            ] = excel_bytes

            st.session_state[
                "prepared_excel_truncated"
            ] = (
                internal_link_count
                > max_excel_links
            )

excel_bytes = st.session_state.get(
    "prepared_excel_bytes"
)

if excel_bytes:
    st.download_button(
        "Download Excel (.xlsx)",
        data=excel_bytes,
        file_name="frustway_seo_crawl.xlsx",
        mime=(
            "application/"
            "vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        use_container_width=True,
        key="download_prepared_excel",
    )

    if st.session_state.get(
        "prepared_excel_truncated"
    ):
        st.warning(
            "大規模サイトのためExcelのInternal Linksシートは"
            "先頭200,000件までです。"
            "クロールDBには全件保存されています。"
        )

st.markdown("#### SEO Spider-style CSV Exports")

spider_export_name = st.selectbox(
    "Export type",
    list(
        SPIDER_EXPORT_SPECS.keys()
    ),
    key="spider_export_type",
)

if spider_export_name == "H2":
    st.caption(
        "H2は Heading extraction = Full outline (H1-H6) "
        "でクロールした場合に取得されます。"
    )

if spider_export_name in (
    "All Inlinks",
    "All Outlinks",
):
    st.caption(
        "Browser版では内部リンクを対象にしています。"
        "大規模サイトでは生成に時間がかかる場合があります。"
    )

if st.button(
    "Prepare selected CSV",
    use_container_width=True,
    key="prepare_spider_export",
):
    safe_name = (
        spider_export_name
        .lower()
        .replace(" ", "_")
        .replace(">", "")
        .replace("/", "_")
    )

    export_path = (
        Path(JOB_DIR)
        / f"export_{safe_name}.csv"
    )

    with st.spinner(
        f"{spider_export_name} を生成しています..."
    ):
        filename, row_count = (
            prepare_spider_export(
                DB_PATH,
                spider_export_name,
                export_path,
            )
        )

    st.session_state[
        "prepared_spider_export_path"
    ] = str(export_path)
    st.session_state[
        "prepared_spider_export_name"
    ] = filename
    st.session_state[
        "prepared_spider_export_rows"
    ] = int(row_count)

prepared_export_path = st.session_state.get(
    "prepared_spider_export_path"
)

if prepared_export_path:
    prepared_path = Path(
        prepared_export_path
    )

    if prepared_path.exists():
        st.caption(
            "Prepared: "
            f"{int(st.session_state.get('prepared_spider_export_rows') or 0):,}"
            " rows"
        )

        with open(
            prepared_path,
            "rb",
        ) as export_fh:
            st.download_button(
                "Download prepared CSV",
                data=export_fh,
                file_name=(
                    st.session_state.get(
                        "prepared_spider_export_name"
                    )
                    or prepared_path.name
                ),
                mime="text/csv",
                use_container_width=True,
                key="download_spider_export",
            )

st.caption(
    "5万URL対応では、重いExcel/Link Exportは必要なときだけ生成します。"
)

