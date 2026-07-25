#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TDnet（適時開示情報閲覧サービス）当日開示情報を抓取し、
指定した証券コードでフィルタリングして、Gmail経由でメール送信するスクリプト。

GitHub Actions での実行を想定し、認証情報は環境変数（GitHub Secrets）から読む。
ローカルで試す場合は export で環境変数をセットするか、.env を用意して
python-dotenv 経由で読み込ませてもよい（load_dotenv()はファイルが無ければ何もしない）。

必要な環境変数:
    GMAIL_USER            送信元Gmailアドレス
    GMAIL_APP_PASSWORD    Googleアプリパスワード（16桁）
    RECIPIENT_EMAIL        受信先アドレス（省略時はGMAIL_USER宛）
    TARGET_CODES           カンマ区切りの証券コード（省略時は全件）

5分おきなど高頻度実行を想定し、「前回までに通知済みの開示」を
state/sent_{date}.json に記録し、新規分だけをメール送信する（毎回全件を
再送しないようにするため）。GitHub Actionsで使う場合は、このstateファイルを
runの最後にリポジトリへコミットし直す必要がある（ワークフロー側で対応）。

使い方:
    python3 monitor.py                  # 今日の開示をチェック（新規分のみ通知）
    python3 monitor.py --date 20260724  # 指定日をチェック
    python3 monitor.py --dry-run        # メールを送らず・stateも更新せずコンソール出力のみ
    python3 monitor.py --debug-html     # 生HTMLをdebug.htmlに保存して終了
    python3 monitor.py --force-all      # 新規判定を無視して当日全件を通知（stateは更新される）
"""

import argparse
import datetime as dt
import json
import os
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv

    load_dotenv()  # .env が無ければ何もしない。GitHub Actions ではSecretsが直接環境変数になる
except ImportError:
    pass

BASE_URL = "https://www.release.tdnet.info/inbs/"
LIST_URL_TMPL = BASE_URL + "I_list_{page:03d}_{date}.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

REQUEST_INTERVAL_SEC = 1.5  # サーバー負荷軽減のため、ページ間で少し待つ

STATE_DIR = "state"


def make_key(item: Dict) -> str:
    """開示情報の一意キー。コード+時刻+表題で識別する（同時刻同コード同表題は同一開示とみなす）。"""
    return f'{item["code"]}|{item["time"]}|{item["title"]}'


def state_path(date_str: str) -> str:
    return os.path.join(STATE_DIR, f"sent_{date_str}.json")


def load_sent_keys(date_str: str) -> set:
    path = state_path(date_str)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def save_sent_keys(date_str: str, keys: set) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state_path(date_str), "w", encoding="utf-8") as f:
        json.dump(sorted(keys), f, ensure_ascii=False, indent=2)


def fetch_list_page(date_str: str, page: int) -> Optional[str]:
    """指定日・指定ページの開示一覧HTMLを取得する。存在しなければNoneを返す。"""
    url = LIST_URL_TMPL.format(page=page, date=date_str)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        return None
    resp.encoding = resp.apparent_encoding or "utf-8"
    text = resp.text
    if "kjTitle" not in text and "Ｉ" not in text:
        return None
    return text


def parse_disclosures(html: str, date_str: str) -> List[Dict]:
    """一覧HTMLから開示情報のリストを抽出する。"""
    soup = BeautifulSoup(html, "lxml")
    results = []

    rows = soup.find_all("tr")
    for row in rows:
        time_cell = row.find("td", class_=lambda c: c and "kjTime" in c)
        code_cell = row.find("td", class_=lambda c: c and "kjCode" in c)
        name_cell = row.find("td", class_=lambda c: c and "kjName" in c)
        title_cell = row.find("td", class_=lambda c: c and "kjTitle" in c)
        place_cell = row.find("td", class_=lambda c: c and "kjPlace" in c)

        if not (time_cell and code_cell and name_cell and title_cell):
            continue

        time_text = time_cell.get_text(strip=True)
        code_text = code_cell.get_text(strip=True)
        name_text = name_cell.get_text(strip=True)
        title_text = title_cell.get_text(strip=True)
        place_text = place_cell.get_text(strip=True) if place_cell else ""

        pdf_link = None
        a_tag = title_cell.find("a")
        if a_tag and a_tag.get("href"):
            href = a_tag["href"]
            pdf_link = href if href.startswith("http") else BASE_URL + href

        if not (time_text and code_text and name_text):
            continue

        try:
            dt_obj = dt.datetime.strptime(f"{date_str}{time_text}", "%Y%m%d%H:%M")
        except ValueError:
            dt_obj = None

        results.append(
            {
                "datetime": dt_obj,
                "time": time_text,
                "code": code_text,
                "name": name_text,
                "title": title_text,
                "place": place_text,
                "pdf_url": pdf_link,
            }
        )

    return results


def fetch_all_disclosures(date_str: str, max_pages: int = 20) -> List[Dict]:
    """指定日の全ページを巡回し、開示情報を集約する。"""
    all_items = []
    for page in range(1, max_pages + 1):
        html = fetch_list_page(date_str, page)
        if not html:
            break
        items = parse_disclosures(html, date_str)
        if not items and page > 1:
            break
        all_items.extend(items)
        time.sleep(REQUEST_INTERVAL_SEC)
    return all_items


def filter_by_codes(items: List[Dict], codes: Optional[List[str]]) -> List[Dict]:
    """証券コードでフィルタリングする。codesがNoneまたは空なら全件返す。"""
    if not codes:
        return items
    code_set = {c.strip() for c in codes if c.strip()}
    return [it for it in items if it["code"] in code_set]


def build_email_html(items: List[Dict], date_str: str) -> str:
    date_fmt = f"{date_str[0:4]}/{date_str[4:6]}/{date_str[6:8]}"
    if not items:
        return (
            f"<html><body>"
            f"<h2>TDnet 開示情報 {date_fmt}</h2>"
            f"<p>該当する開示はありませんでした。</p>"
            f"</body></html>"
        )

    rows_html = ""
    for it in items:
        pdf_html = f'<a href="{it["pdf_url"]}">PDF</a>' if it["pdf_url"] else ""
        rows_html += (
            "<tr>"
            f'<td style="padding:4px 8px;">{it["time"]}</td>'
            f'<td style="padding:4px 8px;">{it["code"]}</td>'
            f'<td style="padding:4px 8px;">{it["name"]}</td>'
            f'<td style="padding:4px 8px;">{it["title"]}</td>'
            f'<td style="padding:4px 8px;">{it["place"]}</td>'
            f'<td style="padding:4px 8px;">{pdf_html}</td>'
            "</tr>"
        )

    html = f"""
    <html>
    <body style="font-family: -apple-system, sans-serif;">
      <h2>TDnet 開示情報 {date_fmt}（{len(items)}件）</h2>
      <table style="border-collapse: collapse; width: 100%;" border="1">
        <thead>
          <tr style="background:#f2f2f2;">
            <th style="padding:4px 8px;">時刻</th>
            <th style="padding:4px 8px;">コード</th>
            <th style="padding:4px 8px;">会社名</th>
            <th style="padding:4px 8px;">表題</th>
            <th style="padding:4px 8px;">取引所</th>
            <th style="padding:4px 8px;">PDF</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </body>
    </html>
    """
    return html


def send_email(subject: str, html_body: str) -> None:
    gmail_user = os.environ["GMAIL_USER"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("RECIPIENT_EMAIL", gmail_user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, [recipient], msg.as_string())


def debug_dump_html(date_str: str, page: int = 1, out_path: str = "debug.html") -> None:
    """パースがうまくいかない時、実際のHTMLを保存して構造を確認するためのヘルパー。"""
    html = fetch_list_page(date_str, page)
    if html is None:
        print("該当ページが取得できませんでした（開示なし、または日付が古すぎる可能性）。")
        return
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTMLを {out_path} に保存しました。テーブルの class 名を確認してください。")


def main():
    parser = argparse.ArgumentParser(description="TDnet開示情報をGmailで通知する")
    parser.add_argument(
        "--date",
        default=dt.date.today().strftime("%Y%m%d"),
        help="対象日 (YYYYMMDD)。省略時は今日（実行環境のローカル日付）。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="メールを送信せず、結果をコンソールに表示するだけ",
    )
    parser.add_argument(
        "--debug-html",
        action="store_true",
        help="対象日1ページ目のHTMLをdebug.htmlに保存して終了",
    )
    parser.add_argument(
        "--force-all",
        action="store_true",
        help="新規判定を無視して、フィルタ後の当日全件を通知する",
    )
    args = parser.parse_args()

    if args.debug_html:
        debug_dump_html(args.date)
        return

    codes_env = os.environ.get("TARGET_CODES", "").strip()
    codes = [c.strip() for c in codes_env.split(",")] if codes_env else None

    print(f"[INFO] {args.date} の開示情報を取得中...")
    all_items = fetch_all_disclosures(args.date)
    print(f"[INFO] 全{len(all_items)}件取得。フィルタ対象コード: {codes or '全件'}")

    filtered = filter_by_codes(all_items, codes)
    print(f"[INFO] フィルタ後: {len(filtered)}件")

    # 前回までに通知済みのキーと比較し、新規分だけを抽出する
    sent_keys = load_sent_keys(args.date)
    current_keys = {make_key(it) for it in filtered}

    if args.force_all:
        new_items = filtered
    else:
        new_items = [it for it in filtered if make_key(it) not in sent_keys]

    print(f"[INFO] 新規: {len(new_items)}件（累計既知: {len(sent_keys)}件）")
    for it in new_items:
        print(f'  [NEW] {it["time"]}  {it["code"]}  {it["name"]}  {it["title"]}')

    if args.dry_run:
        print("[INFO] --dry-run のためメール送信・state更新をスキップしました。")
        return

    if not new_items:
        print("[INFO] 新規の開示はありませんでした。メール送信をスキップします。")
        save_sent_keys(args.date, current_keys | sent_keys)
        return

    subject = f"TDnet開示情報 {args.date}（新規{len(new_items)}件）"
    html_body = build_email_html(new_items, args.date)

    try:
        send_email(subject, html_body)
        print("[INFO] メール送信完了。")
    except KeyError as e:
        print(f"[ERROR] 環境変数が設定されていません: {e}")
        sys.exit(1)
    except smtplib.SMTPAuthenticationError:
        print(
            "[ERROR] Gmail認証に失敗しました。GMAIL_APP_PASSWORD が"
            "Googleアカウントの「アプリパスワード」になっているか確認してください。"
        )
        sys.exit(1)

    # 送信成功後にstateを更新（送信前に更新すると、送信失敗時にその分が
    # 二度と通知されなくなってしまうため）
    save_sent_keys(args.date, current_keys | sent_keys)


if __name__ == "__main__":
    main()
