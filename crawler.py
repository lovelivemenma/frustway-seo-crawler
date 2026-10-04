import argparse
import csv
import ipaddress
import json
import socket
import sqlite3
import time
from collections import deque
from datetime import datetime
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import httpx
from bs4 import BeautifulSoup


TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "yclid",
    "mc_cid",
    "mc_eid",
}

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
    ".ico", ".bmp", ".avif",
    ".pdf",
    ".css", ".js", ".json", ".xml",
    ".mp4", ".webm", ".mov", ".avi",
    ".mp3", ".wav", ".ogg",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".zip", ".gz", ".tar", ".rar", ".7z",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)

USER_AGENT = "Mozilla/5.0 (compatible; FRUSTWAY-SEOCrawler/1.9)"


class PublicTargetBlocked(ValueError):
    pass


def validate_public_url(url):
    """
    Browser公開版用のSSRF軽減チェック。
    http/https・標準ポート・公開IPのみ許可する。
    """
    try:
        parts = urlsplit(url)
    except Exception as exc:
        raise PublicTargetBlocked(
            "URLを解析できません。"
        ) from exc

    if parts.scheme.lower() not in (
        "http",
        "https",
    ):
        raise PublicTargetBlocked(
            "http/https以外はクロールできません。"
        )

    if parts.username or parts.password:
        raise PublicTargetBlocked(
            "認証情報を含むURLはクロールできません。"
        )

    hostname = (
        parts.hostname or ""
    ).strip().lower()

    if not hostname:
        raise PublicTargetBlocked(
            "ホスト名がありません。"
        )

    blocked_names = {
        "localhost",
        "localhost.localdomain",
    }

    if (
        hostname in blocked_names
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
    ):
        raise PublicTargetBlocked(
            "ローカル/内部ホストはクロールできません。"
        )

    try:
        port = parts.port
    except ValueError as exc:
        raise PublicTargetBlocked(
            "ポート番号が不正です。"
        ) from exc

    if port is not None and port not in (
        80,
        443,
    ):
        raise PublicTargetBlocked(
            "公開版では80/443以外のポートは利用できません。"
        )

    def ensure_global_ip(ip_text):
        try:
            ip = ipaddress.ip_address(
                ip_text
            )
        except ValueError as exc:
            raise PublicTargetBlocked(
                "IPアドレスを確認できません。"
            ) from exc

        if not ip.is_global:
            raise PublicTargetBlocked(
                "プライベート/ローカルIPはクロールできません。"
            )

    # IP直書きURL
    try:
        literal_ip = ipaddress.ip_address(
            hostname
        )
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        ensure_global_ip(
            str(literal_ip)
        )
        return True

    try:
        ascii_host = hostname.encode(
            "idna"
        ).decode("ascii")

        infos = socket.getaddrinfo(
            ascii_host,
            port or (
                443
                if parts.scheme.lower()
                == "https"
                else 80
            ),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise PublicTargetBlocked(
            "ホスト名をDNS解決できません。"
        ) from exc

    resolved_ips = {
        info[4][0]
        for info in infos
        if info
        and len(info) >= 5
        and info[4]
    }

    if not resolved_ips:
        raise PublicTargetBlocked(
            "接続先IPを確認できません。"
        )

    for resolved_ip in resolved_ips:
        ensure_global_ip(
            resolved_ip
        )

    return True


def normalize_url(base_url, href):
    try:
        url = urljoin(base_url, href)
        parts = urlsplit(url)

        if parts.scheme.lower() not in ("http", "https"):
            return None

        query_items = parse_qsl(parts.query, keep_blank_values=True)
        query_items = [
            (key, value)
            for key, value in query_items
            if key.lower() not in TRACKING_PARAMS
        ]

        clean_query = urlencode(query_items, doseq=True)

        # 日本語URLとパーセントエンコードURLを同一形式へ正規化
        decoded_path = unquote(parts.path or "/")
        normalized_path = quote(
            decoded_path,
            safe="/:@!$&'()*+,;=-._~",
        )

        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                normalized_path,
                clean_query,
                "",
            )
        )
    except Exception:
        return None


def host_key(hostname):
    if not hostname:
        return ""

    hostname = hostname.lower()

    if hostname.startswith("www."):
        hostname = hostname[4:]

    return hostname


def same_site(url, root_hostname):
    try:
        return host_key(urlsplit(url).hostname) == host_key(root_hostname)
    except Exception:
        return False


def is_crawlable_url(url):
    try:
        path = urlsplit(url).path.lower()
        return not path.endswith(SKIP_EXTENSIONS)
    except Exception:
        return False


def get_meta_content(soup, name):
    tag = soup.find(
        "meta",
        attrs={
            "name": lambda x: isinstance(x, str)
            and x.lower() == name.lower()
        },
    )

    if not tag:
        return ""

    return (tag.get("content") or "").strip()


def get_canonical(soup, current_url):
    for tag in soup.find_all("link"):
        rel = tag.get("rel")

        if not rel:
            continue

        rel_values = (
            [str(x).lower() for x in rel]
            if isinstance(rel, list)
            else str(rel).lower().split()
        )

        if "canonical" not in rel_values:
            continue

        href = (tag.get("href") or "").strip()

        if not href:
            return ""

        return normalize_url(current_url, href) or href

    return ""


def clean_body_text(soup):
    """
    HTMLを再パースせず、本文テキストだけ抽出する。
    大規模クロール時のCPU/メモリ消費を抑える。
    """
    excluded = {
        "script",
        "style",
        "noscript",
        "svg",
        "template",
    }

    parts = []

    for text in soup.stripped_strings:
        parent = getattr(text, "parent", None)
        parent_name = getattr(parent, "name", "")

        if parent_name in excluded:
            continue

        parts.append(str(text))

    return " ".join(parts)


def extract_headings(soup, heading_mode):
    """
    basic: H1のみ
    full: H1〜H6をHTML上の出現順で取得
    """
    h1_tags = soup.find_all("h1")
    h1_list = [
        tag.get_text(" ", strip=True)
        for tag in h1_tags
    ]

    result = {
        "h1": " || ".join(h1_list),
        "h1_count": len(h1_list),
        "h2": "",
        "h2_count": 0,
        "h3_count": 0,
        "h4_count": 0,
        "h5_count": 0,
        "h6_count": 0,
        "headings_json": "[]",
        "headings_outline": "",
    }

    if heading_mode != "full":
        return result

    headings = []
    counts = {
        1: 0,
        2: 0,
        3: 0,
        4: 0,
        5: 0,
        6: 0,
    }
    h2_list = []
    outline_lines = []

    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = tag.get_text(" ", strip=True)

        if not text:
            continue

        level = int(tag.name[1])
        counts[level] += 1

        if level == 2:
            h2_list.append(text)

        headings.append(
            {
                "level": level,
                "text": text,
            }
        )

        indent = "  " * (level - 1)
        outline_lines.append(
            f"{indent}H{level}: {text}"
        )

    result.update(
        {
            "h1_count": counts[1],
            "h2": " || ".join(h2_list),
            "h2_count": counts[2],
            "h3_count": counts[3],
            "h4_count": counts[4],
            "h5_count": counts[5],
            "h6_count": counts[6],
            "headings_json": json.dumps(
                headings,
                ensure_ascii=False,
            ),
            "headings_outline": "\n".join(outline_lines),
        }
    )

    return result


def contains_noindex(meta_robots, x_robots_tag):
    directives = f"{meta_robots},{x_robots_tag}".lower()
    return "noindex" in directives


def evaluate_indexability(
    url,
    status_code,
    content_type,
    meta_robots,
    x_robots_tag,
    canonical,
):
    if status_code != 200:
        return "No", f"HTTP Status {status_code}"

    if "text/html" not in content_type.lower():
        return "No", "Non-HTML"

    if contains_noindex(meta_robots, x_robots_tag):
        return "No", "Noindex"

    if canonical:
        normalized_canonical = normalize_url(url, canonical)
        normalized_url = normalize_url(url, url)

        if normalized_canonical and normalized_canonical != normalized_url:
            return "No", "Canonicalised"

    return "Yes", ""


def canonical_match_value(url, canonical):
    if not canonical:
        return "Missing"

    normalized_url = normalize_url(url, url)
    normalized_canonical = normalize_url(url, canonical)

    if normalized_url == normalized_canonical:
        return "Yes"

    return "No"


def ensure_columns(conn):
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(pages)").fetchall()
    }

    wanted = {
        "heading_mode": "TEXT",
        "h3_count": "INTEGER DEFAULT 0",
        "h4_count": "INTEGER DEFAULT 0",
        "h5_count": "INTEGER DEFAULT 0",
        "h6_count": "INTEGER DEFAULT 0",
        "headings_json": "TEXT DEFAULT '[]'",
        "headings_outline": "TEXT DEFAULT ''",
        "parent_url": "TEXT DEFAULT ''",
    }

    for name, definition in wanted.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE pages ADD COLUMN {name} {definition}"
            )

    conn.commit()


def init_database(db_path):
    conn = sqlite3.connect(
        db_path,
        timeout=60,
    )

    # 5万URL級を想定したSQLite設定。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-131072")
    conn.execute("PRAGMA busy_timeout=60000")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            status_code INTEGER,
            content_type TEXT,
            title TEXT,
            title_length INTEGER,
            description TEXT,
            description_length INTEGER,
            robots TEXT,
            x_robots_tag TEXT,
            canonical TEXT,
            canonical_match TEXT,
            indexable TEXT,
            indexability_reason TEXT,
            h1 TEXT,
            h1_count INTEGER,
            h2 TEXT,
            h2_count INTEGER,
            h3_count INTEGER DEFAULT 0,
            h4_count INTEGER DEFAULT 0,
            h5_count INTEGER DEFAULT 0,
            h6_count INTEGER DEFAULT 0,
            headings_json TEXT DEFAULT '[]',
            headings_outline TEXT DEFAULT '',
            heading_mode TEXT,
            character_count INTEGER,
            body_text TEXT,
            depth INTEGER,
            parent_url TEXT DEFAULT '',
            response_time REAL,
            redirect_url TEXT,
            crawled_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT,
            target_url TEXT,
            anchor_text TEXT,
            is_internal INTEGER
        )
        """
    )

    ensure_columns(conn)

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(status_code)",
        "CREATE INDEX IF NOT EXISTS idx_pages_depth ON pages(depth)",
        "CREATE INDEX IF NOT EXISTS idx_pages_indexable ON pages(indexable)",
        "CREATE INDEX IF NOT EXISTS idx_pages_parent ON pages(parent_url)",
        "CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_url)",
        "CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_url)",
        (
            "CREATE INDEX IF NOT EXISTS idx_links_internal_source "
            "ON links(is_internal, source_url)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_links_internal_target "
            "ON links(is_internal, target_url)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_links_source_target "
            "ON links(source_url, target_url)"
        ),
    ]

    for sql in indexes:
        conn.execute(sql)

    conn.commit()
    return conn


def reset_database(conn):
    conn.execute("DELETE FROM links")
    conn.execute("DELETE FROM pages")
    conn.commit()


def save_page(conn, data):
    conn.execute(
        """
        INSERT OR REPLACE INTO pages (
            url,
            status_code,
            content_type,
            title,
            title_length,
            description,
            description_length,
            robots,
            x_robots_tag,
            canonical,
            canonical_match,
            indexable,
            indexability_reason,
            h1,
            h1_count,
            h2,
            h2_count,
            h3_count,
            h4_count,
            h5_count,
            h6_count,
            headings_json,
            headings_outline,
            heading_mode,
            character_count,
            body_text,
            depth,
            parent_url,
            response_time,
            redirect_url,
            crawled_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?
        )
        """,
        (
            data["url"],
            data["status_code"],
            data["content_type"],
            data["title"],
            data["title_length"],
            data["description"],
            data["description_length"],
            data["robots"],
            data["x_robots_tag"],
            data["canonical"],
            data["canonical_match"],
            data["indexable"],
            data["indexability_reason"],
            data["h1"],
            data["h1_count"],
            data["h2"],
            data["h2_count"],
            data["h3_count"],
            data["h4_count"],
            data["h5_count"],
            data["h6_count"],
            data["headings_json"],
            data["headings_outline"],
            data["heading_mode"],
            data["character_count"],
            data["body_text"],
            data["depth"],
            data["parent_url"],
            data["response_time"],
            data["redirect_url"],
            data["crawled_at"],
        ),
    )


def save_links(conn, rows):
    if not rows:
        return

    conn.executemany(
        """
        INSERT INTO links (
            source_url,
            target_url,
            anchor_text,
            is_internal
        )
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )


def save_link(conn, source_url, target_url, anchor_text, is_internal):
    save_links(
        conn,
        [
            (
                source_url,
                target_url,
                anchor_text,
                1 if is_internal else 0,
            )
        ],
    )


def blank_page_data(
    url,
    depth,
    status_code=0,
    heading_mode="basic",
    parent_url="",
):
    return {
        "url": url,
        "status_code": status_code,
        "content_type": "",
        "title": "",
        "title_length": 0,
        "description": "",
        "description_length": 0,
        "robots": "",
        "x_robots_tag": "",
        "canonical": "",
        "canonical_match": "Missing",
        "indexable": "No",
        "indexability_reason": (
            "Request Error"
            if status_code == 0
            else f"HTTP Status {status_code}"
        ),
        "h1": "",
        "h1_count": 0,
        "h2": "",
        "h2_count": 0,
        "h3_count": 0,
        "h4_count": 0,
        "h5_count": 0,
        "h6_count": 0,
        "headings_json": "[]",
        "headings_outline": "",
        "heading_mode": heading_mode,
        "character_count": 0,
        "body_text": "",
        "depth": depth,
        "parent_url": parent_url,
        "response_time": 0.0,
        "redirect_url": "",
        "crawled_at": datetime.now().isoformat(timespec="seconds"),
    }


def crawl(
    start_url,
    max_pages,
    delay,
    db_path,
    heading_mode="basic",
    progress_callback=None,
    should_stop=None,
    store_body_text=True,
    store_external_links=False,
    commit_every=25,
    public_mode=False,
):
    start_url = normalize_url(start_url, start_url)

    if not start_url:
        raise ValueError("開始URLが不正です。")

    if public_mode:
        validate_public_url(
            start_url
        )

    if heading_mode not in ("basic", "full"):
        raise ValueError("heading_mode must be 'basic' or 'full'")

    root_hostname = urlsplit(start_url).hostname

    queue = deque([(start_url, 0, "")])
    queued = {start_url}
    visited = set()

    conn = init_database(db_path)
    reset_database(conn)

    client = httpx.Client(
        follow_redirects=False,
        timeout=20,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
        },
    )

    crawled = 0
    stopped = False
    pages_since_commit = 0

    def notify(event, **payload):
        if not progress_callback:
            return

        try:
            progress_callback(
                {
                    "event": event,
                    "crawled": crawled,
                    "max_pages": max_pages,
                    "queued": len(queue),
                    "discovered": len(queued),
                    **payload,
                }
            )
        except Exception:
            # UI側の進捗表示エラーでクロール本体を止めない
            pass

    def stop_requested():
        if not should_stop:
            return False

        try:
            return bool(should_stop())
        except Exception:
            return False

    notify(
        "start",
        current_url="",
        depth=0,
        status_code=None,
    )

    print()
    print("======================================")
    print(" FRUSTWAY SEO Crawler v1.9")
    print("======================================")
    print(f"Start URL    : {start_url}")
    print(f"Max pages    : {max_pages}")
    print(f"Heading mode : {heading_mode}")
    print(f"Store body   : {store_body_text}")
    print(f"External     : {store_external_links}")
    print(f"Public mode  : {public_mode}")
    print(f"Database     : {db_path}")
    print()

    while queue and crawled < max_pages:
        if stop_requested():
            stopped = True
            break

        current_url, depth, parent_url = queue.popleft()

        if current_url in visited:
            continue

        visited.add(current_url)

        notify(
            "page_start",
            current_url=current_url,
            depth=depth,
            status_code=None,
        )

        print(
            f"[{crawled + 1}/{max_pages}] "
            f"Depth {depth} | {current_url}"
        )

        started = time.perf_counter()
        page_status = 0

        try:
            if public_mode:
                validate_public_url(
                    current_url
                )

            response = client.get(current_url)
            response_time = time.perf_counter() - started

            status = response.status_code
            page_status = status
            content_type = response.headers.get("content-type", "")
            x_robots_tag = response.headers.get("x-robots-tag", "").strip()

            data = blank_page_data(
                current_url,
                depth,
                status_code=status,
                heading_mode=heading_mode,
                parent_url=parent_url,
            )

            data["content_type"] = content_type
            data["x_robots_tag"] = x_robots_tag
            data["response_time"] = round(response_time, 3)
            data["crawled_at"] = datetime.now().isoformat(
                timespec="seconds"
            )

            if 300 <= status <= 399:
                location = response.headers.get("location", "").strip()

                if location:
                    redirect_url = normalize_url(
                        current_url,
                        location,
                    )

                    if redirect_url:
                        data["redirect_url"] = redirect_url

                        if (
                            same_site(redirect_url, root_hostname)
                            and is_crawlable_url(redirect_url)
                            and redirect_url not in visited
                            and redirect_url not in queued
                        ):
                            queue.append(
                                (
                                    redirect_url,
                                    depth,
                                    current_url,
                                )
                            )
                            queued.add(redirect_url)

                data["indexability_reason"] = f"HTTP Status {status}"

            elif status == 200 and "text/html" in content_type.lower():
                soup = BeautifulSoup(response.text, "lxml")

                title = (
                    soup.title.get_text(" ", strip=True)
                    if soup.title
                    else ""
                )

                description = get_meta_content(
                    soup,
                    "description",
                )

                robots = get_meta_content(
                    soup,
                    "robots",
                )

                canonical = get_canonical(
                    soup,
                    current_url,
                )

                heading_data = extract_headings(
                    soup,
                    heading_mode,
                )

                body_text = clean_body_text(soup)

                indexable, indexability_reason = evaluate_indexability(
                    current_url,
                    status,
                    content_type,
                    robots,
                    x_robots_tag,
                    canonical,
                )

                data.update(
                    {
                        "title": title,
                        "title_length": len(title),
                        "description": description,
                        "description_length": len(description),
                        "robots": robots,
                        "canonical": canonical,
                        "canonical_match": canonical_match_value(
                            current_url,
                            canonical,
                        ),
                        "indexable": indexable,
                        "indexability_reason": indexability_reason,
                        "character_count": len(body_text),
                        "body_text": (
                            body_text
                            if store_body_text
                            else ""
                        ),
                    }
                )

                data.update(heading_data)

                link_rows = []

                for a in soup.find_all("a", href=True):
                    href = (a.get("href") or "").strip()

                    if not href:
                        continue

                    target = normalize_url(
                        current_url,
                        href,
                    )

                    if not target:
                        continue

                    anchor = a.get_text(
                        " ",
                        strip=True,
                    )

                    internal = same_site(
                        target,
                        root_hostname,
                    )

                    if internal or store_external_links:
                        link_rows.append(
                            (
                                current_url,
                                target,
                                anchor,
                                1 if internal else 0,
                            )
                        )

                    if (
                        internal
                        and is_crawlable_url(target)
                        and target not in visited
                        and target not in queued
                    ):
                        queue.append(
                            (
                                target,
                                depth + 1,
                                current_url,
                            )
                        )
                        queued.add(target)

                save_links(
                    conn,
                    link_rows,
                )

            else:
                indexable, reason = evaluate_indexability(
                    current_url,
                    status,
                    content_type,
                    "",
                    x_robots_tag,
                    "",
                )
                data["indexable"] = indexable
                data["indexability_reason"] = reason

            save_page(conn, data)
            pages_since_commit += 1

            if pages_since_commit >= max(
                int(commit_every),
                1,
            ):
                conn.commit()
                pages_since_commit = 0

        except Exception as exc:
            response_time = time.perf_counter() - started

            print(
                f"  ERROR: {type(exc).__name__}: {exc}"
            )

            data = blank_page_data(
                current_url,
                depth,
                status_code=0,
                heading_mode=heading_mode,
                parent_url=parent_url,
            )

            data["response_time"] = round(response_time, 3)

            if isinstance(
                exc,
                PublicTargetBlocked,
            ):
                data[
                    "indexability_reason"
                ] = (
                    "Blocked by public safety policy"
                )

            save_page(conn, data)
            pages_since_commit += 1

            if pages_since_commit >= max(
                int(commit_every),
                1,
            ):
                conn.commit()
                pages_since_commit = 0

        crawled += 1

        notify(
            "page_done",
            current_url=current_url,
            depth=depth,
            status_code=page_status,
        )

        if delay > 0:
            remaining = float(delay)

            # Stop要求へ早めに反応できるよう、小刻みに待機する。
            while remaining > 0:
                if stop_requested():
                    stopped = True
                    break

                sleep_for = min(0.1, remaining)
                time.sleep(sleep_for)
                remaining -= sleep_for

            if stopped:
                break

    conn.commit()

    if stop_requested():
        stopped = True

    if stopped:
        final_event = "stopped"
    elif crawled >= max_pages and queue:
        # Max pagesに達したが、まだ未処理URLが残っている。
        # 「完了」と区別してUI側で部分クロールであることを明示する。
        final_event = "limit_reached"
    else:
        final_event = "complete"

    notify(
        final_event,
        current_url="",
        depth=0,
        status_code=None,
    )

    client.close()
    return conn


def duplicate_titles(conn):
    rows = conn.execute(
        """
        SELECT title
        FROM pages
        WHERE status_code = 200
          AND TRIM(title) != ''
        GROUP BY title
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    return {row[0] for row in rows}


def issues_for_row(row, duplicate_title_set):
    issues = []

    status = row["status_code"]
    title = row["title"]
    description = row["description"]
    h1_count = row["h1_count"]
    canonical_match = row["canonical_match"]
    indexability_reason = row["indexability_reason"]

    if status == 0:
        if (
            indexability_reason
            == "Blocked by public safety policy"
        ):
            issues.append("Blocked Target")
        else:
            issues.append("Request Error")
    elif 300 <= status <= 399:
        issues.append("Redirect")
    elif status == 404:
        issues.append("404 Not Found")
    elif 400 <= status <= 499:
        issues.append(f"{status} Client Error")
    elif status >= 500:
        issues.append(f"{status} Server Error")

    if status == 200:
        if not title.strip():
            issues.append("Missing Title")
        elif title in duplicate_title_set:
            issues.append("Duplicate Title")

        if not description.strip():
            issues.append("Missing Meta Description")

        if h1_count == 0:
            issues.append("Missing H1")
        elif h1_count > 1:
            issues.append("Multiple H1")

        if canonical_match == "Missing":
            issues.append("Missing Canonical")
        elif canonical_match == "No":
            issues.append("Canonicalised")

        if indexability_reason == "Noindex":
            issues.append("Noindex")

    return " | ".join(issues)


def export_csv(conn, output_path):
    dup_titles = duplicate_titles(conn)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        WITH inlink_counts AS (
            SELECT
                target_url,
                COUNT(DISTINCT source_url) AS cnt
            FROM links
            WHERE is_internal = 1
            GROUP BY target_url
        ),
        outlink_counts AS (
            SELECT
                source_url,
                COUNT(DISTINCT target_url) AS cnt
            FROM links
            WHERE is_internal = 1
            GROUP BY source_url
        )
        SELECT
            p.url,
            p.status_code,
            p.content_type,
            p.indexable,
            p.indexability_reason,
            p.title,
            p.title_length,
            p.description,
            p.description_length,
            p.robots,
            p.x_robots_tag,
            p.canonical,
            p.canonical_match,
            p.h1,
            p.h1_count,
            p.h2,
            p.h2_count,
            p.h3_count,
            p.h4_count,
            p.h5_count,
            p.h6_count,
            p.headings_outline,
            p.heading_mode,
            p.character_count,
            p.depth,
            p.response_time,
            p.redirect_url,
            COALESCE(i.cnt, 0) AS unique_internal_inlinks,
            COALESCE(o.cnt, 0) AS unique_internal_outlinks,
            p.crawled_at
        FROM pages p
        LEFT JOIN inlink_counts i
            ON i.target_url = p.url
        LEFT JOIN outlink_counts o
            ON o.source_url = p.url
        ORDER BY p.id
        """
    ).fetchall()

    columns = [
        "URL",
        "Status",
        "Content Type",
        "Indexable",
        "Indexability Reason",
        "Title",
        "Title Length",
        "Description",
        "Description Length",
        "Robots",
        "X-Robots-Tag",
        "Canonical",
        "Canonical Match",
        "H1",
        "H1 Count",
        "H2",
        "H2 Count",
        "H3 Count",
        "H4 Count",
        "H5 Count",
        "H6 Count",
        "Headings Outline",
        "Heading Mode",
        "Character Count",
        "Depth",
        "Response Time",
        "Redirect URL",
        "Unique Internal Inlinks",
        "Unique Internal Outlinks",
        "Issues",
        "Crawled At",
    ]

    with open(
        output_path,
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as f:
        writer = csv.writer(f)
        writer.writerow(columns)

        for row in rows:
            issues = issues_for_row(
                row,
                dup_titles,
            )

            writer.writerow(
                [
                    row["url"],
                    row["status_code"],
                    row["content_type"],
                    row["indexable"],
                    row["indexability_reason"],
                    row["title"],
                    row["title_length"],
                    row["description"],
                    row["description_length"],
                    row["robots"],
                    row["x_robots_tag"],
                    row["canonical"],
                    row["canonical_match"],
                    row["h1"],
                    row["h1_count"],
                    row["h2"],
                    row["h2_count"],
                    row["h3_count"],
                    row["h4_count"],
                    row["h5_count"],
                    row["h6_count"],
                    row["headings_outline"],
                    row["heading_mode"],
                    row["character_count"],
                    row["depth"],
                    row["response_time"],
                    row["redirect_url"],
                    row["unique_internal_inlinks"],
                    row["unique_internal_outlinks"],
                    issues,
                    row["crawled_at"],
                ]
            )

    conn.row_factory = None


def print_summary(conn):
    total = conn.execute(
        "SELECT COUNT(*) FROM pages"
    ).fetchone()[0]

    status_200 = conn.execute(
        "SELECT COUNT(*) FROM pages WHERE status_code = 200"
    ).fetchone()[0]

    redirects = conn.execute(
        """
        SELECT COUNT(*)
        FROM pages
        WHERE status_code BETWEEN 300 AND 399
        """
    ).fetchone()[0]

    errors_404 = conn.execute(
        "SELECT COUNT(*) FROM pages WHERE status_code = 404"
    ).fetchone()[0]

    server_errors = conn.execute(
        "SELECT COUNT(*) FROM pages WHERE status_code >= 500"
    ).fetchone()[0]

    missing_titles = conn.execute(
        """
        SELECT COUNT(*)
        FROM pages
        WHERE status_code = 200
          AND TRIM(title) = ''
        """
    ).fetchone()[0]

    duplicate_title_sets = conn.execute(
        """
        SELECT COUNT(*)
        FROM (
            SELECT title
            FROM pages
            WHERE status_code = 200
              AND TRIM(title) != ''
            GROUP BY title
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]

    missing_h1 = conn.execute(
        """
        SELECT COUNT(*)
        FROM pages
        WHERE status_code = 200
          AND h1_count = 0
        """
    ).fetchone()[0]

    multiple_h1 = conn.execute(
        """
        SELECT COUNT(*)
        FROM pages
        WHERE status_code = 200
          AND h1_count > 1
        """
    ).fetchone()[0]

    noindex = conn.execute(
        """
        SELECT COUNT(*)
        FROM pages
        WHERE indexability_reason = 'Noindex'
        """
    ).fetchone()[0]

    canonicalised = conn.execute(
        """
        SELECT COUNT(*)
        FROM pages
        WHERE indexability_reason = 'Canonicalised'
        """
    ).fetchone()[0]

    print()
    print("======================================")
    print(" Crawl Complete")
    print("======================================")
    print(f"Pages crawled        : {total}")
    print(f"200 OK               : {status_200}")
    print(f"3xx                  : {redirects}")
    print(f"404                  : {errors_404}")
    print(f"5xx                  : {server_errors}")
    print(f"Missing title        : {missing_titles}")
    print(f"Duplicate title sets : {duplicate_title_sets}")
    print(f"Missing H1           : {missing_h1}")
    print(f"Multiple H1          : {multiple_h1}")
    print(f"Noindex              : {noindex}")
    print(f"Canonicalised        : {canonicalised}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="FRUSTWAY SEO Crawler"
    )

    parser.add_argument("url", help="クロール開始URL")

    parser.add_argument(
        "--max-pages",
        type=int,
        default=1000,
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
    )

    parser.add_argument(
        "--db",
        default="seo.db",
    )

    parser.add_argument(
        "--csv",
        default="crawl.csv",
    )

    parser.add_argument(
        "--heading-mode",
        choices=["basic", "full"],
        default="basic",
        help="basic=Title+H1 / full=H1-H6",
    )

    parser.add_argument(
        "--no-body-text",
        action="store_true",
        help="本文をDBへ保存しない（大規模クロール向け）",
    )

    parser.add_argument(
        "--store-external-links",
        action="store_true",
        help="外部リンクもDBへ保存する",
    )

    parser.add_argument(
        "--public-mode",
        action="store_true",
        help="公開Web版向けのSSRF軽減チェックを有効化",
    )

    args = parser.parse_args()

    conn = crawl(
        args.url,
        args.max_pages,
        args.delay,
        args.db,
        heading_mode=args.heading_mode,
        store_body_text=not args.no_body_text,
        store_external_links=args.store_external_links,
        public_mode=args.public_mode,
    )

    export_csv(conn, args.csv)
    print_summary(conn)

    print(f"SQLite : {args.db}")
    print(f"CSV    : {args.csv}")
    print()

    conn.close()


if __name__ == "__main__":
    main()
