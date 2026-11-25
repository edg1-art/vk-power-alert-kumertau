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
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
LOCAL_CONFIG_PATH = os.path.join(ROOT, "config.local.json")
STATE_PATH = os.path.join(ROOT, "state.json")
HISTORY_MD_PATH = os.path.join(ROOT, "ИСТОРИЯ-ОТКЛЮЧЕНИЙ.md")

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

MENU_KEYBOARD = {
    "keyboard": [
        ["📰 Последние отключения", "🕒 Когда отключали"],
        ["🔄 Проверить сейчас", "ℹ️ Статус"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

MENU_VERSION = 2

BOT_COMMANDS = [
    {"command": "start", "description": "Меню и кнопки"},
    {"command": "news", "description": "Последние отключения по Авиаторов"},
    {"command": "last", "description": "Когда последний раз отключали"},
    {"command": "status", "description": "Статус бота"},
    {"command": "check", "description": "Проверить ВК сейчас"},
]


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


def post_hits(post, cfg):
    """Улицы записи: у сохранённой истории они лежат готовым списком."""
    streets = post.get("streets")
    if isinstance(streets, list):
        return streets
    return street_hits(post.get("text") or "", cfg)


def post_url(cfg, post):
    return "https://vk.com/wall-%d_%d" % (int(cfg["group_id"]), post["id"])


def build_message(post, cfg, hits):
    link = post_url(cfg, post)
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


def tg_send(token, chat_id, text, reply_markup=None):
    params = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    res = tg_call(token, "sendMessage", params)
    if not res.get("ok"):
        raise RuntimeError("Telegram отказ: %s" % (res.get("description") or res))
    return res


# ---------------------------------------------------------------------------
#  Меню бота: кнопки, команды, опрос Telegram
# ---------------------------------------------------------------------------

def state_patch(**updates):
    """Обновляет отдельные ключи state.json, не теряя остальные."""
    cur = load_json(STATE_PATH, {}) or {}
    cur.update(updates)
    cur.pop("last_run", None)
    save_json(STATE_PATH, cur)
    return cur


def short_text(post, limit=300):
    t = re.sub(r"\s+", " ", post.get("text") or "").strip()
    if len(t) > limit:
        t = t[:limit].rstrip() + "…"
    return t


def outage_posts(posts, cfg):
    return [p for p in posts if is_outage(p.get("text") or "", cfg)]


RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]

MONTH_NUM = {name: i + 1 for i, name in enumerate(RU_MONTHS)}


def parse_date_iso(text):
    """«2 ноября 2026 (дата в тексте)» -> «2026-11-02»."""
    m = re.search(r"(\d{1,2})\s+(%s)\s+(\d{4})" % "|".join(RU_MONTHS),
                  (text or "").lower())
    if not m:
        return ""
    return "%04d-%02d-%02d" % (int(m.group(3)), MONTH_NUM[m.group(2)], int(m.group(1)))


def days_ago(date_iso):
    if not date_iso:
        return ""
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    except ValueError:
        return ""
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5))).date()
    n = (today - d).days
    if n <= 0:
        return "сегодня"
    if n == 1:
        return "вчера"
    if n % 10 == 1 and n % 100 != 11:
        word = "день"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "дня"
    else:
        word = "дней"
    return "%d %s назад" % (n, word)


def history_key(item):
    return (item.get("date_iso") or "0000-00-00", int(item["id"]))


def fmt_iso(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return ""
    return "%d %s %d" % (d.day, RU_MONTHS[d.month - 1], d.year)


def text_date_iso(text, base_iso=""):
    """Ищет дату в тексте записи: «2 ноября 2026» или «26 октября»."""
    m = re.search(r"(\d{1,2})\s+(%s)(?:\s+(\d{4}))?" % "|".join(RU_MONTHS),
                  (text or "").lower())
    if not m:
        return ""
    day, month = int(m.group(1)), MONTH_NUM[m.group(2)]
    year = m.group(3) or (base_iso or "")[:4]
    if not year:
        return ""
    return "%04d-%02d-%02d" % (int(year), month, day)


def event_date_iso(item):
    """Дата отключения: из текста, иначе дата самой записи."""
    return (text_date_iso(item.get("text"), item.get("date_iso") or "")
            or item.get("date_iso") or "")


def event_key(item):
    return (event_date_iso(item) or "0000-00-00", int(item["id"]))


def event_when(item):
    """«2 ноября 2026, 10:00–17:00 (запись от 31 октября 2026)»."""
    iso = event_date_iso(item)
    post = item.get("date_iso") or ""
    note = ""
    if iso and post and iso != post:
        note = "запись от " + fmt_iso(post)
    elif "завтра" in (item.get("text") or "").lower():
        note = "в тексте «завтра»"
    when = fmt_iso(iso) if iso else "дата неизвестна"
    tr = time_range(item.get("text") or "")
    if tr:
        when += ", " + tr
    if note:
        when += " (" + note + ")"
    return when


def time_range(text):
    m = re.search(r"(\d{1,2})[:.](\d{2})\s*(?:до|—|–|-)\s*(\d{1,2})[:.](\d{2})",
                  text or "")
    if not m:
        return ""
    return "%s:%s–%s:%s" % (m.group(1), m.group(2), m.group(3), m.group(4))


def street_addresses(text, cfg):
    """Только адреса с нашей улицей, без остального текста."""
    names = cfg.get("streets") or []
    if not names:
        return ""
    m = re.search(r"адрес[а-я]*\s*:?\s*(.*)", text or "", re.I | re.S)
    body = m.group(1) if m else (text or "")
    found = []
    for raw in re.split(r";|\n", body):
        part = raw.strip(" -–—•\t.")
        part = re.sub(r"\.\s*(?:Это связано|В связи с|Причина|Работы)[^.]*$", "",
                      part, flags=re.I).strip(" -–—.")
        low = normalize(part)
        if part and any(normalize(n) in low for n in names):
            part = re.sub(r"\b(ул|пер|просп|б-р|д|кв)\.\s*(?=[А-ЯЁA-Za-z0-9])",
                          r"\1. ", part)
            found.append(re.sub(r"\s+", " ", part))
    return "; ".join(dict.fromkeys(found))


def short_when(item):
    iso = event_date_iso(item)
    when = fmt_iso(iso) if iso else "дата неизвестна"
    tr = time_range(item.get("text") or "")
    if tr:
        when += " (%s)" % tr
    return when


def absolute_date(widget_date, now=None):
    """«вчера в 7:36» -> «24 ноября 2026, 07:36» (чтобы история не старела)."""
    s = (widget_date or "").strip()
    m = re.match(r"(вчера|сегодня|завтра)\s+в\s+(\d{1,2}):(\d{2})", s, re.I)
    if not m:
        return s
    base = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=3)))
    shift = {"вчера": -1, "сегодня": 0, "завтра": 1}[m.group(1).lower()]
    d = base + timedelta(days=shift)
    return "%d %s %d, %s:%s" % (d.day, RU_MONTHS[d.month - 1], d.year, m.group(2), m.group(3))


def update_history(posts, cfg):
    """Копит все отключения (для ответа «когда отключали» глубже 10 записей)."""
    st = load_json(STATE_PATH, {}) or {}
    hist = {int(h["id"]): h for h in (st.get("history") or [])
            if isinstance(h, dict) and h.get("id")}
    changed = False
    for p in outage_posts(posts, cfg):
        if int(p["id"]) in hist:
            continue
        hist[int(p["id"])] = {
            "id": p["id"],
            "date": absolute_date(p.get("date")),
            "date_iso": parse_date_iso(absolute_date(p.get("date"))),
            "text": short_text(p, 700),
            "streets": post_hits(p, cfg),
        }
        changed = True
    if changed:
        items = sorted(hist.values(), key=lambda h: int(h["id"]))[-60:]
        state_patch(history=items)


def known_outages(cfg, posts=None):
    st = load_json(STATE_PATH, {}) or {}
    hist = {}
    for h in (st.get("history") or []):
        if isinstance(h, dict) and h.get("id"):
            h = dict(h)
            if not h.get("date_iso"):
                h["date_iso"] = parse_date_iso(h.get("date"))
            hist[int(h["id"])] = h
    for p in outage_posts(posts or [], cfg):
        hist[int(p["id"])] = {
            "id": p["id"],
            "date": p.get("date") or "",
            "date_iso": parse_date_iso(absolute_date(p.get("date"))),
            "text": short_text(p, 700),
            "streets": post_hits(p, cfg),
        }
    return sorted(hist.values(), key=history_key)


def filter_by_street(posts, cfg):
    """В режиме only оставляет записи, где есть наша улица."""
    if cfg.get("street_mode", "mark") != "only":
        return posts
    return [p for p in posts if post_hits(p, cfg)]


def street_phrase(cfg):
    streets = cfg.get("streets") or []
    return ("по ул. " + ", ".join(streets)) if streets else "по вашей улице"


def digest_recent_outages(cfg, posts, limit=5):
    outs = filter_by_street(known_outages(cfg, posts), cfg)
    if not outs:
        return ("📰 Отключений %s пока не было.\nБот следит с 25.11.2026."
                % street_phrase(cfg))
    lines = ["📰 Последние отключения %s:" % street_phrase(cfg), ""]
    for p in sorted(outs, key=event_key, reverse=True)[:limit]:
        lines.append("🗓 " + event_when(p))
        addr = street_addresses(p.get("text") or "", cfg)
        if addr:
            lines.append("📍 " + addr)
        lines.append("🔗 " + post_url(cfg, p))
        lines.append("")
    return "\n".join(lines).strip()


def digest_last(cfg, posts):
    outs = filter_by_street(known_outages(cfg, posts), cfg)
    if not outs:
        return ("🕒 Пока не знаю: отключений %s ещё не видел.\nБот следит с 25.11.2026."
                % street_phrase(cfg))
    ordered = sorted(outs, key=event_key)
    last = ordered[-1]
    lines = ["🕒 Последнее отключение %s: %s."
             % (street_phrase(cfg), event_when(last)),
             "🔗 " + post_url(cfg, last)]
    dates = []
    for h in reversed(ordered[:-1]):
        d = short_when(h)
        if d and d not in dates:
            dates.append(d)
    if dates:
        lines.append("До этого: " + ", ".join(dates[:3]) + ".")
    return "\n".join(lines)
    last = max(outs, key=lambda p: p["id"])
    hits = street_hits(last.get("text") or "", cfg)
    lines = ["🕒 Последнее отключение:", "", "🗓 " + (last.get("date") or "—")]
    if hits:
        lines.append("⚡ Улица: " + ", ".join(hits))
    lines += ["", short_text(last, 1200), "", "🔗 " + post_url(cfg, last)]
    older = sorted(outs, key=lambda p: p["id"])[:-1]
    if older:
        lines += ["", "До этого: " + ", ".join((p.get("date") or "—") for p in older[-3:])]
    return "\n".join(lines)


def digest_status(cfg, posts):
    lines = [
        "ℹ️ Бот работает",
        "",
        "Проверка ВК: каждую минуту",
        "Слежу: отключения %s" % street_phrase(cfg),
    ]
    outs = filter_by_street(known_outages(cfg, posts), cfg)
    if outs:
        lo = max(outs, key=event_key)
        d = fmt_iso(event_date_iso(lo)) or "дата неизвестна"
        lines.append("Последнее отключение: %s" % d)
    else:
        lines.append("Последнее отключение: пока не видел")
    lines += ["", "Кнопки: /start"]
    return "\n".join(lines)


def save_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def write_history_md(cfg):
    """Человекочитаемая история отключений — обновляется автоматически."""
    items = sorted(filter_by_street(known_outages(cfg), cfg),
                   key=event_key, reverse=True)
    lines = [
        "# История отключений электроэнергии",
        "",
        "Файл обновляется ботом автоматически. Показаны отключения %s."
        % street_phrase(cfg),
        "",
        "| Дата | Время | Адрес | Запись ВК |",
        "|---|---|---|---|",
    ]
    for it in items:
        when = fmt_iso(event_date_iso(it)) or "—"
        tr = time_range(it.get("text") or "") or "—"
        addr = (street_addresses(it.get("text") or "", cfg) or "—").replace("|", "/")
        lines.append("| %s | %s | %s | [#%d](%s) |"
                     % (when, tr, addr, int(it["id"]), post_url(cfg, it)))
    if not items:
        lines.append("| — | — | — | — |")
    lines += ["", "Всего: %d." % len(items), ""]
    save_text(HISTORY_MD_PATH, "\n".join(lines))


def compute_new(posts, state, force_new_id=None):
    pool_max = max(p["id"] for p in posts) if posts else 0
    prev_max = int(state.get("max_id") or 0)
    if force_new_id is not None:
        prev_max = force_new_id
    return prev_max, pool_max, [p for p in posts if p["id"] > prev_max]


def pick_to_send(new_posts, state, cfg):
    sent_ids = set(state.get("sent") or [])
    to_send = []
    for p in new_posts:
        if p["id"] in sent_ids:
            log("  пропуск %d: уже отправлялась раньше" % p["id"])
            continue
        if not is_outage(p["text"], cfg):
            log("  пропуск %d: не про отключение электроэнергии" % p["id"])
            continue
        hits = post_hits(p, cfg)
        mode = cfg.get("street_mode", "mark")
        if mode == "only" and not hits:
            log("  пропуск %d: нет улицы из списка (режим only)" % p["id"])
            continue
        to_send.append((p, build_message(p, cfg, hits if mode != "off" else [])))
    return to_send


def send_notifications(cfg, token, chat_id, to_send):
    for post, msg in to_send:
        markup = {"inline_keyboard": [[
            {"text": "🔗 Открыть запись в ВК", "url": post_url(cfg, post)}
        ]]}
        tg_send(token, chat_id, msg, markup)
        log("Отправлено уведомление по записи %d" % post["id"])


def handle_check_now(cfg, token, chat_id, vk_token):
    posts, _raw = get_posts(cfg, vk_token)
    update_history(posts, cfg)
    write_history_md(cfg)
    state = load_json(STATE_PATH, {}) or {}
    prev_max, pool_max, new_posts = compute_new(posts, state)
    if prev_max <= 0:
        state_patch(max_id=pool_max, sent=[])
        return "Первый запуск: запомнил текущие записи, дальше буду присылать новое."
    if not new_posts:
        return "🔄 Новых записей в сообществе нет."
    to_send = pick_to_send(new_posts, state, cfg)
    if to_send:
        send_notifications(cfg, token, chat_id, to_send)
    sent = sorted(set(list(state.get("sent") or []) + [p["id"] for p, _ in to_send]))[-200:]
    state_patch(max_id=max(pool_max, prev_max), sent=sent)
    if not to_send:
        return ("🔄 Новых записей: %d. Отключений %s среди них нет."
                % (len(new_posts), street_phrase(cfg)))
    return ("🔄 Новых записей: %d. Отправил уведомлений: %d."
            % (len(new_posts), len(to_send)))


def tg_handle_update(token, chat_id, cfg, vk_token, update):
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    if str(chat.get("id")) != str(chat_id):
        log("Telegram: сообщение из чужого чата %s — игнорирую" % chat.get("id"))
        return False
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    low = normalize(text)

    if low.startswith("/start") or low == "меню":
        tg_send(token, chat_id,
                "🤖 Бот «Отключения Кумертау»\n\n"
                "Слежу за сообществом ВК и присылаю новые записи об отключениях "
                "электроэнергии. Проверка каждую минуту.\n\n"
                "Кнопки — внизу 👇", MENU_KEYBOARD)
        return True

    if (text in ("📰 Последние отключения", "📰 Последние новости", "⚡ Отключения")
            or low.startswith("/news") or low.startswith("/outages")):
        posts, _ = get_posts(cfg, vk_token)
        tg_send(token, chat_id, digest_recent_outages(cfg, posts))
        return True

    if text == "🕒 Когда отключали" or low.startswith("/last"):
        posts, _ = get_posts(cfg, vk_token)
        tg_send(token, chat_id, digest_last(cfg, posts))
        return True

    if text == "ℹ️ Статус" or low.startswith("/status"):
        posts, _ = get_posts(cfg, vk_token)
        tg_send(token, chat_id, digest_status(cfg, posts))
        return True

    if text == "🔄 Проверить сейчас" or low.startswith("/check"):
        tg_send(token, chat_id, handle_check_now(cfg, token, chat_id, vk_token))
        return True

    tg_send(token, chat_id, "Не понял 🙂 Нажмите кнопку внизу или отправьте /start.")
    return True


def poll_telegram(token, chat_id, cfg, vk_token):
    state = load_json(STATE_PATH, {}) or {}
    offset = int(state.get("tg_offset") or 0)
    params = {"timeout": 0, "limit": 100}
    if offset:
        params["offset"] = offset
    res = tg_call(token, "getUpdates", params)
    if not res.get("ok"):
        log("Telegram getUpdates: %s" % (res.get("description") or res))
        return
    updates = res.get("result") or []
    if not updates:
        log("Telegram: новых сообщений нет")
        return
    new_offset = offset
    seen = set()
    for upd in updates:
        new_offset = max(new_offset, int(upd.get("update_id") or 0) + 1)
        msg = upd.get("message") or {}
        key = (str((msg.get("chat") or {}).get("id")), (msg.get("text") or "").strip())
        if key[1] and key in seen:
            continue          # одинаковые команды в одной пачке — отвечаем один раз
        seen.add(key)
        try:
            tg_handle_update(token, chat_id, cfg, vk_token, upd)
        except Exception as e:
            log("Ошибка обработки команды: %s" % e)
            try:
                tg_send(token, chat_id, "⚠️ Ошибка при выполнении команды: %s" % e)
            except Exception:
                pass
    state_patch(tg_offset=new_offset)
    log("Telegram: обработано сообщений: %d, offset=%d" % (len(updates), new_offset))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="ничего не отправлять и не сохранять")
    ap.add_argument("--test-telegram", action="store_true", help="отправить тестовое сообщение")
    ap.add_argument("--reset", action="store_true", help="сбросить состояние")
    ap.add_argument("--force-new-id", type=int, default=None,
                    help="считать, что последняя известная запись имела этот id")
    ap.add_argument("--verbose", action="store_true", help="показать все записи и результат фильтра")
    ap.add_argument("--poll", action="store_true",
                    help="опросить Telegram: обработать кнопки и команды (без проверки ВК)")
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

    if args.poll:
        st = load_json(STATE_PATH, {}) or {}
        if st.get("menu_v") != MENU_VERSION:
            try:
                tg_send(token, chat_id,
                        "🤖 Обновил меню: показываю только отключения электроэнергии %s. "
                        "Кнопки — внизу 👇" % street_phrase(cfg),
                        MENU_KEYBOARD)
                state_patch(menu_v=MENU_VERSION)
                log("Меню обновлено.")
            except Exception as e:
                log("Не смог отправить меню: %s" % e)
        if st.get("commands_v") != MENU_VERSION:
            try:
                tg_call(token, "setMyCommands",
                        {"commands": json.dumps(BOT_COMMANDS, ensure_ascii=False)})
                state_patch(commands_v=MENU_VERSION)
                log("Команды бота обновлены.")
            except Exception as e:
                log("setMyCommands: %s" % e)
        poll_telegram(token, chat_id, cfg, vk_token)
        return 0

    posts, _raw = get_posts(cfg, vk_token)
    if not posts:
        log("ОШИБКА: список записей пуст")
        return 1
    if not args.dry_run:
        update_history(posts, cfg)
        write_history_md(cfg)

    state = load_json(STATE_PATH, {}) or {}
    prev_max, pool_max, new_posts = compute_new(posts, state, args.force_new_id)

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
            state_patch(max_id=pool_max, sent=[])
            if token and chat_id and cfg.get("notify_on_start", True):
                text = (
                    "✅ Бот запущен и следит за сообществом\n"
                    "%s\n\n"
                    "Проверка каждую минуту.\n"
                    "Показываю: отключения %s.\n"
                    "Сейчас в ленте %d записей, стартовое состояние сохранено.\n\n"
                    "Кнопки — внизу 👇"
                    % (cfg.get("community_url", ""),
                       street_phrase(cfg),
                       len(posts))
                )
                tg_send(token, chat_id, text, MENU_KEYBOARD)
                log("Стартовое сообщение отправлено в Telegram.")
        return 0

    log("Новых записей: %d" % len(new_posts))

    to_send = pick_to_send(new_posts, state, cfg)

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
        send_notifications(cfg, token, chat_id, to_send)

    if not args.dry_run:
        sent = list(state.get("sent") or [])
        for post, _msg in to_send:
            sent.append(post["id"])
        sent = sorted(set(sent))[-200:]
        state_patch(max_id=max(pool_max, prev_max), sent=sent)
        log("Состояние сохранено: max_id=%d" % max(pool_max, prev_max))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("ОШИБКА: %s" % e)
        sys.exit(1)
