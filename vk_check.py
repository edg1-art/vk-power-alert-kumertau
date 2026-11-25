#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Мониторинг записей сообщества ВК «Город Кумертау» об отключениях электроэнергии.
Отправляет уведомления в Telegram. Рассчитан на запуск по расписанию GitHub Actions.

Зависимостей нет — только стандартная библиотека Python 3.9+.

Режимы запуска:
  python vk_check.py                 обычная проверка (для GitHub Actions)
  python vk_check.py --dry-run       только показать, ничего не отправлять и не сохранять
  python vk_check.py --test-telegram отправить тестовое сообщение в Telegram
  python vk_check.py --reset         забыть сохранённое состояние
  python vk_check.py --force-new-id N считать, что последняя известная запись была N
"""

import argparse
import html as html_lib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
LOCAL_CONFIG_PATH = os.path.join(ROOT, "config.local.json")
STATE_PATH = os.path.join(ROOT, "state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

WIDGET_URLS = [
    "https://vk.com/widget_community.php?app=0&width=300px&_ver=1&gid={gid}&mode=4",
    "https://vk.ru/widget_community.php?app=0&width=300px&_ver=1&gid={gid}&mode=4",
    "https://m.vk.com/widget_community.php?app=0&width=300px&_ver=1&gid={gid}&mode=4",
]

EMOJI_RX = re.compile(r'<img[^>]*?alt="&#(\d+);"[^>]*?>')
TEXT_RX = re.compile(r'class="wall_post_text"[^>]*>(.*?)</div>', re.S)
DATE_RX = re.compile(r'class="wcommunity_post_date"[^>]*>([^<]+)<')
BUTTON_RX = re.compile(r'<button[^>]*>.*?</button>', re.S)


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + str(msg), flush=True)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        log("ВНИМАНИЕ: не смог прочитать %s: %s" % (path, e))
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def decode_html(raw):
    for enc in ("utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1251", errors="replace")


def _emoji_sub(m):
    try:
        return chr(int(m.group(1)))
    except (ValueError, OverflowError):
        return ""


def extract_text(block):
    m = TEXT_RX.search(block)
    if not m:
        return ""
    s = m.group(1)
    s = BUTTON_RX.sub(" ", s)          # «Показать полностью» — не нужен
    s = EMOJI_RX.sub(_emoji_sub, s)    # картинки-эмодзи -> символы
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    s = html_lib.unescape(s)
    s = s.replace('\xa0', ' ').replace('\u200b', '')
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n\s*\n+', '\n\n', s)
    lines = [ln.strip() for ln in s.split('\n')]
    return '\n'.join(lines).strip()


def parse_widget(text, gid):
    posts = {}
    parts = text.split('<div class="wcommunity_post">')[1:]
    for block in parts:
        m = re.search(r'id="wpt-%d_(\d+)"' % gid, block)
        if not m:
            continue
        pid = int(m.group(1))
        date = DATE_RX.search(block)
        posts[pid] = {
            "id": pid,
            "date": date.group(1).strip() if date else "",
            "text": extract_text(block),
            "source": "widget",
        }
    return [posts[k] for k in sorted(posts)]


def fetch_from_api(gid, token):
    url = "https://api.vk.com/method/wall.get?" + urllib.parse.urlencode({
        "owner_id": -gid, "count": 20, "filter": "owner", "v": "5.199",
        "access_token": token,
    })
    status, raw = fetch(url, timeout=30)
    data = json.loads(raw.decode("utf-8"))
    if "error" in data:
        raise RuntimeError("VK API: %s" % data["error"].get("error_msg"))
    posts = []
    for item in data.get("response", {}).get("items", []):
        if not isinstance(item, dict) or "text" not in item:
            continue
        posts.append({
            "id": int(item["id"]),
            "date": datetime.fromtimestamp(item.get("date", 0), timezone.utc).strftime("%d.%m.%Y %H:%M UTC"),
            "text": item.get("text", "").strip(),
            "source": "api",
        })
    return sorted(posts, key=lambda p: p["id"])


def get_posts(cfg, vk_token=None):
    gid = int(cfg["group_id"])
    errors = []
    for tpl in WIDGET_URLS:
        url = tpl.format(gid=gid)
        try:
            status, raw = fetch(url)
        except Exception as e:
            errors.append("%s -> %s" % (url.split("?")[0], e))
            continue
        text = decode_html(raw)
        posts = parse_widget(text, gid)
        if posts:
            log("ВК: получено %d записей (%s), HTTP %s" % (len(posts), url.split("?")[0], status))
            return posts, text
        lowered = text.lower()
        if "captcha" in lowered or "security check" in lowered:
            errors.append("%s -> капча/проверка безопасности" % url.split("?")[0])
        else:
            errors.append("%s -> HTTP %s, записей не найдено (len=%d)" % (url.split("?")[0], status, len(text)))
    if vk_token:
        log("Пробую запасной путь через VK API...")
        try:
            posts = fetch_from_api(gid, vk_token)
            if posts:
                log("VK API: получено %d записей" % len(posts))
                return posts, ""
        except Exception as e:
            errors.append("VK API -> %s" % e)
    raise RuntimeError("Не удалось получить записи ВК:\n  " + "\n  ".join(errors))


def normalize(s):
    s = (s or "").replace("\u00a0", " ").replace("ё", "е").replace("Ё", "Е")
    return re.sub(r"\s+", " ", s).strip().lower()


def is_outage(text, cfg):
    n = normalize(text)
    all_kw = [normalize(k) for k in cfg.get("keywords_all", [])]
    any_kw = [normalize(k) for k in cfg.get("keywords_any", [])]
    if not all(k in n for k in all_kw):
        return False
    return all(k in n for k in any_kw) if any_kw else True


def street_hits(text, cfg):
    n = normalize(text)
    return [s for s in cfg.get("streets", []) if normalize(s) and normalize(s) in n]


def build_message(post, cfg, hits):
    link = "https://vk.com/wall-%d_%d" % (int(cfg["group_id"]), post["id"])
    body = post["text"].strip()
    if len(body) > 3400:
        body = body[:3400].rstrip() + "…"
    if hits:
        head = "⚡ ВАША УЛИЦА (%s) · отключение электроэнергии" % ", ".join(hits)
    else:
        head = "🔔 Отключение электроэнергии"
    parts = [head, "", body]
    if post.get("date"):
        parts.append("")
        parts.append("🕒 " + post["date"])
    parts.append("🔗 " + link)
    return "\n".join(parts).strip()


def tg_call(token, method, params=None, timeout=30):
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            raise RuntimeError("Telegram HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise RuntimeError("Telegram недоступен: %s" % e.reason)


def tg_send(token, chat_id, text):
    res = tg_call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    })
    if not res.get("ok"):
        raise RuntimeError("Telegram отказ: %s" % (res.get("description") or res))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="ничего не отправлять и не сохранять")
    ap.add_argument("--test-telegram", action="store_true", help="отправить тестовое сообщение")
    ap.add_argument("--reset", action="store_true", help="сбросить состояние")
    ap.add_argument("--force-new-id", type=int, default=None,
                    help="считать, что последняя известная запись имела этот id")
    ap.add_argument("--verbose", action="store_true", help="показать все записи и результат фильтра")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, {}) or {}
    local = load_json(LOCAL_CONFIG_PATH, {}) or {}
    if local:
        cfg.update({k: v for k, v in local.items() if k in ("group_id", "streets", "street_mode")})
    token = os.environ.get("TELEGRAM_TOKEN") or local.get("telegram_token") or ""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or local.get("telegram_chat_id") or ""
    vk_token = os.environ.get("VK_TOKEN") or local.get("vk_token") or ""

    if not cfg.get("group_id"):
        log("ОШИБКА: в config.json не задан group_id")
        return 1

    if not args.dry_run and (not token or not chat_id):
        log("ОШИБКА: не заданы TELEGRAM_TOKEN/TELEGRAM_CHAT_ID — "
            "не буду трогать состояние, чтобы не потерять записи")
        return 1

    if args.test_telegram:
        if not token or not chat_id:
            log("ОШИБКА: нужны TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")
            return 1
        tg_send(token, chat_id, "✅ Тест связи: бот «Отключения Кумертау» работает.")
        log("Тестовое сообщение отправлено.")
        return 0

    if args.reset and os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
        log("Состояние сброшено.")

    posts, _raw = get_posts(cfg, vk_token)
    if not posts:
        log("ОШИБКА: список записей пуст")
        return 1

    pool_max = max(p["id"] for p in posts)
    state = load_json(STATE_PATH, {}) or {}
    prev_max = int(state.get("max_id") or 0)
    if args.force_new_id is not None:
        prev_max = args.force_new_id

    log("Диапазон записей: %d..%d, сохранённый max_id=%s"
        % (min(p["id"] for p in posts), pool_max, prev_max or "нет"))

    if args.verbose:
        for p in posts:
            hits = street_hits(p["text"], cfg)
            flags = []
            if is_outage(p["text"], cfg):
                flags.append("ОТКЛЮЧЕНИЕ")
            if hits:
                flags.append("УЛИЦА:" + ",".join(hits))
            preview = re.sub(r"\s+", " ", p["text"])[:90]
            log("  %d | %-22s | %s | %s" % (p["id"], p["date"][:22], ",".join(flags) or "-", preview))

    # Первый запуск: запоминаем текущие записи и не спамим старыми
    if prev_max <= 0:
        log("Первый запуск: стартовое состояние = %d, старые записи не отправляю." % pool_max)
        if not args.dry_run:
            save_json(STATE_PATH, {
                "max_id": pool_max,
                "sent": [],
            })
            if token and chat_id and cfg.get("notify_on_start", True):
                text = (
                    "✅ Бот запущен и следит за сообществом\n"
                    "%s\n\n"
                    "Проверка каждые 5 минут.\n"
                    "Улица для пометки: %s (режим: %s).\n"
                    "Сейчас в ленте %d записей, стартовое состояние сохранено."
                    % (cfg.get("community_url", ""),
                       ", ".join(cfg.get("streets", [])) or "не задана",
                       cfg.get("street_mode", "mark"),
                       len(posts))
                )
                tg_send(token, chat_id, text)
                log("Стартовое сообщение отправлено в Telegram.")
        return 0

    new_posts = [p for p in posts if p["id"] > prev_max]
    log("Новых записей: %d" % len(new_posts))

    sent_ids = set(state.get("sent") or [])
    to_send = []
    for p in new_posts:
        if p["id"] in sent_ids:
            log("  пропуск %d: уже отправлялась раньше" % p["id"])
            continue
        if not is_outage(p["text"], cfg):
            log("  пропуск %d: не про отключение электроэнергии" % p["id"])
            continue
        hits = street_hits(p["text"], cfg)
        mode = cfg.get("street_mode", "mark")
        if mode == "only" and not hits:
            log("  пропуск %d: нет улицы из списка (режим only)" % p["id"])
            continue
        to_send.append((p, build_message(p, cfg, hits if mode != "off" else [])))

    if len(new_posts) >= 10:
        log("ВНИМАНИЕ: за интервал появилось 10+ записей — часть могла не попасть в виджет")

    if not to_send:
        log("Подходящих новых записей нет — уведомления не отправляются.")
    elif args.dry_run:
        for post, msg in to_send:
            log("--- сообщение по записи %d (dry-run, не отправлено) ---" % post["id"])
            print(msg)
    else:
        if not token or not chat_id:
            log("ОШИБКА: нет TELEGRAM_TOKEN/TELEGRAM_CHAT_ID — некуда отправлять")
            return 1
        for post, msg in to_send:
            tg_send(token, chat_id, msg)
            log("Отправлено уведомление по записи %d" % post["id"])

    if not args.dry_run:
        sent = list(state.get("sent") or [])
        for post, _msg in to_send:
            sent.append(post["id"])
        sent = sorted(set(sent))[-200:]
        save_json(STATE_PATH, {
            "max_id": max(pool_max, prev_max),
            "sent": sent,
        })
        log("Состояние сохранено: max_id=%d" % max(pool_max, prev_max))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("ОШИБКА: %s" % e)
        sys.exit(1)
