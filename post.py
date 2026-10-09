"""Автопостинг новостей кино и сериалов в ВК-группу «СтопКадр».

Каждый запуск: свежие новости из RSS -> Gemini выбирает одну и пишет пост ->
фото со страницы новости (или из RSS) -> загрузка фото в ВК -> пост на стене.
Опубликованные новости запоминаются в seen.json, чтобы не повторяться.

Запуск: python post.py            — опубликовать один пост
        python post.py --dry-run  — только показать, что было бы опубликовано (без Gemini и ВК)
Нужны переменные окружения VK_TOKEN и GEMINI_API_KEY (в GitHub — раздел Secrets).
"""
import html as htmllib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

# ---------- Настройки ----------
GROUP_ID = "237783609"
FEEDS = [
    "https://variety.com/v/film/feed/",
    "https://deadline.com/feed/",
    "https://www.kinonews.ru/rss/",
    "https://www.film.ru/rss.xml",
    "https://kg-portal.ru/rss/news.rss",
]
MAX_AGE_HOURS = 24
SLIDES = 3
GEMINI_MODELS = [os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash-lite", "gemini-2.5-flash"]
VK_API = "5.199"
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130 Safari/537.36"

PROMPT = """Ты профессиональный редактор сообщества ВКонтакте «СтопКадр» о кино и сериалах.

Ниже список свежих новостей в JSON. Выбери ОДНУ самую интересную новость именно про кино или сериалы (не игры, не музыку, не политику). Предпочитай громкие премьеры, трейлеры, кастинг, продолжения, сборы и рейтинги.

Напиши по ней пост на русском языке.
Правила:
1. headline — короткий цепляющий заголовок до 90 символов: интрига, конкретика, сильный глагол. Без лжи и выдуманных деталей.
2. text — 2–3 небольших абзаца, максимум 700 символов. В конце — вопрос для обсуждения.
3. Не выдумывай факты, даты, имена и цитаты. Переводи с английского на естественный русский.
4. Без шаблонных фраз и канцелярита. Не более двух эмодзи на весь пост.
5. hashtags — 3–4 хэштега на русском.

Ответь ТОЛЬКО JSON без пояснений:
{"index": номер новости, "headline": "...", "text": "...", "hashtags": ["#..."]}

Новости:
"""


# ---------- Сеть ----------
def http(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def vk(method, **params):
    data = urllib.parse.urlencode({**params, "v": VK_API}).encode()
    res = json.loads(http(f"https://api.vk.com/method/{method}", data,
                          {"Authorization": "Bearer " + os.environ["VK_TOKEN"]}))
    if "error" in res:
        raise RuntimeError(f"ВК {method}: {res['error'].get('error_msg')}")
    return res["response"]


# ---------- Новости ----------
def strip(v):
    v = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", v or "")
    v = htmllib.unescape(htmllib.unescape(v))  # бывает двойное экранирование (film.ru)
    v = re.sub(r"<[^>]+>", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def tag(block, name):
    m = re.search(rf"<{name}[^>]*>([\s\S]*?)</{name}>", block, re.I)
    return strip(m.group(1)) if m else ""


def parse_date(s):
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fresh_news(seen):
    now = datetime.now(timezone.utc)
    news = {}
    for feed in FEEDS:
        try:
            xml = http(feed, timeout=15).decode("utf-8", "ignore")
        except Exception as e:
            print(f"  ! {feed} не ответил: {e}")
            continue
        for block in re.findall(r"<item[\s>][\s\S]*?</item>", xml, re.I):
            link, title = tag(block, "link"), tag(block, "title")
            if not link or not title or link in seen:
                continue
            d = parse_date(tag(block, "pubDate"))
            if d and (now - d).total_seconds() > MAX_AGE_HOURS * 3600:
                continue
            img = re.search(r"<(?:enclosure|media:content|media:thumbnail)[^>]*url=[\"']([^\"']+)[\"']", block, re.I)
            news[link] = {"title": title, "summary": tag(block, "description")[:400], "link": link,
                          "date": d.isoformat() if d else "", "image": htmllib.unescape(img.group(1)) if img else "",
                          "_t": d.timestamp() if d else 0}
    items = sorted(news.values(), key=lambda n: -n["_t"])[:25]
    for i, n in enumerate(items):
        n["index"] = i
        n.pop("_t")
    return items


# ---------- Gemini ----------
def write_post(cands):
    body = json.dumps({
        "contents": [{"parts": [{"text": PROMPT + json.dumps(
            [{k: c[k] for k in ("index", "title", "summary", "date")} for c in cands], ensure_ascii=False)}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }).encode()
    last = None
    for model in GEMINI_MODELS:
        try:
            raw = http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", body,
                       {"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]}, 90)
            text = json.loads(raw)["candidates"][0]["content"]["parts"][0]["text"]
            r = json.loads(re.search(r"\{[\s\S]*\}", text).group(0))
            pick = next((c for c in cands if c["index"] == int(r.get("index", -1))), cands[0])
            tags = " ".join(r["hashtags"]) if isinstance(r.get("hashtags"), list) else (r.get("hashtags") or "")
            return pick, f"{r['headline']}\n\n{r['text']}\n\n{tags}\n\nИсточник: {pick['link']}".strip()
        except urllib.error.HTTPError as e:
            last = f"{model}: HTTP {e.code} {e.read()[:300]!r}"
            print("  ! Gemini", last)
        except Exception as e:
            last = f"{model}: {e}"
            print("  ! Gemini", last)
    raise RuntimeError("Gemini не ответил: " + str(last))


# ---------- Фото ----------
JUNK = re.compile(r"logo|icon|avatar|sprite|favicon|banner|counter|pixel|placeholder|fallback|blank|emoji|/ads?/|gravatar|1x1|\.svg|\.gif|/special/|rankimg|articles_share|/themes/|/partners/|/\d{2,3}x\d{2,3}\.\w+$", re.I)
SMALL = re.compile(r"podcast|thumb_\d{2,3}x|middle|thumb_600|epsa_|small|preview|mini|[?&]w=[1-5]\d\d\b|-\d{2,3}x\d{2,3}\.", re.I)


def page_images(page_url):
    """Главное фото новости + крупные фото из текста статьи (без превью других новостей и логотипов)."""
    try:
        page = http(page_url, timeout=15).decode("utf-8", "ignore")
    except Exception as e:
        print(f"  ! страница новости не открылась: {e}")
        return []
    urls, keys = [], set()

    def add(u):
        if not u:
            return
        u = urllib.parse.urljoin(page_url, htmllib.unescape(u))
        if not u.startswith("http") or JUNK.search(u):
            return
        k = re.sub(r"\?.*$", "", u)
        k = re.sub(r"-\d+x\d+(?=\.\w+$)", "", k)
        k = re.sub(r"_2x(?=\.\w+$)", "", k)
        k = re.sub(r"/(big|large|l)/", "/", k)
        if k not in keys:
            keys.add(k)
            urls.append(u)

    for m in re.finditer(r"<meta[^>]+(?:property|name)=[\"'](?:og:image|twitter:image)(?::src)?[\"'][^>]*>", page, re.I):
        c = re.search(r"content=[\"']([^\"']+)[\"']", m.group(0), re.I)
        add(c and c.group(1))
    body = (re.search(r"<article[\s\S]*?</article>", page, re.I) or re.search(r"[\s\S]*", page)).group(0)
    for m in re.finditer(r"<img[^>]+>", body, re.I):
        t = m.group(0)
        if re.search(r"class=[\"'][^\"']*(mini|thumb|related|teaser|card)", t, re.I):
            continue
        w = re.search(r"\swidth=[\"']?(\d+)", t, re.I)
        if w and int(w.group(1)) < 600:
            continue
        src = re.search(r"(?:data-src|data-lazy-src|src)=[\"']([^\"']+\.(?:jpe?g|png|webp)[^\"']*)[\"']", t, re.I)
        if src and not SMALL.search(src.group(1)):
            add(src.group(1))
    return urls[:SLIDES]


def as_jpg(url):
    # wsrv.nl переводит любой формат (webp и т.п.) в JPG шириной 1080, который точно примет ВК
    return "https://wsrv.nl/?url=" + urllib.parse.quote(url, safe="") + "&output=jpg&w=1080&q=90&we"


def upload_photos(urls):
    """Загрузка фото на стену группы. Нужен ключ профиля администратора с правом «Фотографии»;
    с ключом сообщества ВК не даёт загружать фото — тогда пост выйдет без фото."""
    try:
        server = vk("photos.getWallUploadServer", group_id=GROUP_ID)["upload_url"]
    except Exception as e:
        print(f"  ! фото пропущены: {e}")
        return []
    out = []
    for i, u in enumerate(urls):
        try:
            img = http(as_jpg(u), timeout=40)
            boundary = "----stopkadr%d" % random.randint(1, 10**9)
            body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"photo{i + 1}.jpg\"\r\n"
                    f"Content-Type: image/jpeg\r\n\r\n").encode() + img + f"\r\n--{boundary}--\r\n".encode()
            up = json.loads(http(server, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}, 60))
            ph = vk("photos.saveWallPhoto", group_id=GROUP_ID, server=up["server"], photo=up["photo"], hash=up["hash"])[0]
            out.append(f"photo{ph['owner_id']}_{ph['id']}")
        except Exception as e:
            print(f"  ! фото {u} не загрузилось: {e}")
    return out


# ---------- Главное ----------
def main():
    dry = "--dry-run" in sys.argv
    seen = json.load(open(SEEN_FILE, encoding="utf-8")) if os.path.exists(SEEN_FILE) else []
    cands = fresh_news(set(seen))
    print(f"Свежих новостей: {len(cands)}")
    if not cands:
        print("Публиковать нечего — ждём следующего запуска.")
        return
    if dry:
        pick, text = cands[0], f"[пробный запуск, текст напишет Gemini]\n\n{cands[0]['title']}\n\nИсточник: {cands[0]['link']}"
    else:
        pick, text = write_post(cands)
    images = page_images(pick["link"]) or ([pick["image"]] if pick.get("image") else [])
    print("Новость:", pick["link"], "\nФото:", images, "\n---\n" + text + "\n---")
    if dry:
        return
    photos = upload_photos(images) if images else []
    res = vk("wall.post", owner_id="-" + GROUP_ID, from_group=1, message=text, attachments=",".join(photos),
             primary_attachments_mode="carousel" if len(photos) > 1 else "grid")
    print(f"Опубликовано: https://vk.ru/wall-{GROUP_ID}_{res['post_id']} (фото: {len(photos)})")
    seen = (seen + [pick["link"]])[-1000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
