"""Автопостинг новостей кино и сериалов в ВК-группу «СтопКадр».

Каждый запуск: свежие новости из RSS -> Gemini выбирает одну и пишет пост ->
фото со страницы новости (или из RSS) -> загрузка фото в ВК -> пост на стене.
Опубликованные новости запоминаются в seen.json, чтобы не повторяться.

Запуск: python post.py            — опубликовать один пост
        python post.py --check-vk — проверить право загрузки фото без публикации
        python post.py --dry-run  — только показать, что было бы опубликовано (без Gemini и ВК)
Нужны VK_TOKEN и GEMINI_API_KEY; для фото можно задать VK_PHOTO_TOKEN (в GitHub — раздел Secrets).
"""
import html as htmllib
import json
import io
from html.parser import HTMLParser

from PIL import Image, ImageOps
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
GEMINI_MODELS = [os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash-lite", "gemini-3.8-flash"]
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


class VKError(RuntimeError):
    def __init__(self, method, error):
        self.code = error.get("error_code")
        hint = ""
        if self.code in (5, 7, 15, 27, 28) and method.startswith("photos."):
            hint = (" Проверьте VK_PHOTO_TOKEN: нужен пользовательский токен "
                    "администратора сообщества с правом photos; ключ сообщества "
                    "может не поддерживать этот метод.")
        super().__init__(f"ВК {method}, код {self.code}: {error.get('error_msg')}.{hint}")


def vk(method, *, token=None, **params):
    data = urllib.parse.urlencode({**params, "v": VK_API}).encode()
    res = json.loads(http(f"https://api.vk.com/method/{method}", data,
                          {"Authorization": "Bearer " + (token or os.environ["VK_TOKEN"])}))
    if "error" in res:
        raise VKError(method, res["error"])
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
        "generationConfig": {
            "responseMimeType": "application/json",
            # строгая схема: Gemini всегда вернёт ровно эти поля
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "index": {"type": "INTEGER"},
                    "headline": {"type": "STRING"},
                    "text": {"type": "STRING"},
                    "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
                "required": ["index", "headline", "text", "hashtags"],
            },
        },
    }).encode()
    last = None
    # каждую модель пробуем дважды: при перегрузке (503) Google советует просто повторить чуть позже
    for model in [m for m in GEMINI_MODELS for _ in range(2)]:
        try:
            raw = http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", body,
                       {"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]}, 90)
            res = json.loads(raw)
            # ответ может состоять из нескольких частей (например, служебной части «размышлений») — берём текст
            parts = (res.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                raise ValueError("нет JSON в ответе: " + json.dumps(res, ensure_ascii=False)[:400])
            r = json.loads(m.group(0))
            if isinstance(r, list):
                r = r[0]
            headline = r.get("headline") or r.get("title") or r.get("заголовок") or ""
            post_body = r.get("text") or r.get("post") or r.get("body") or r.get("summary") or r.get("content") or r.get("текст") or ""
            if not post_body:
                raise ValueError("в ответе нет текста поста: " + text[:400])
            try:
                pick = next(c for c in cands if c["index"] == int(r.get("index", -1)))
            except (StopIteration, ValueError, TypeError):
                pick = cands[0]
            tags = r.get("hashtags") or r.get("tags") or ""
            tags = " ".join(tags) if isinstance(tags, list) else tags
            return pick, "\n\n".join(x for x in (headline, post_body, tags, "Источник: " + pick["link"]) if x)
        except urllib.error.HTTPError as e:
            last = f"{model}: HTTP {e.code} {e.read()[:300]!r}"
            print("  ! Gemini", last)
            if e.code in (429, 500, 503):
                time.sleep(20)
        except Exception as e:
            last = f"{model}: {e}"
            print("  ! Gemini", last)
    raise RuntimeError("Gemini не ответил: " + str(last))


# ---------- Фото ----------
JUNK = re.compile(r"logo|icon|avatar|sprite|favicon|banner|counter|pixel|placeholder|fallback|blank|emoji|/ads?/|gravatar|1x1|\.svg|\.gif|/special/|rankimg|articles_share|/themes/|/partners/|/\d{2,3}x\d{2,3}\.\w+$", re.I)
SMALL = re.compile(r"podcast|thumb_\d{2,3}x|middle|thumb_600|epsa_|small|preview|mini|[?&]w=[1-5]\d\d\b|-\d{2,3}x\d{2,3}\.", re.I)


class ImageParser(HTMLParser):
    """Атрибуты HTML могут идти в любом порядке; расширение URL необязательно."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.images = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            name = (attrs.get("property") or attrs.get("name") or "").lower()
            if name in ("og:image", "og:image:url", "og:image:secure_url",
                        "twitter:image", "twitter:image:src"):
                self.meta.append(attrs.get("content"))
        if tag in ("img", "source"):
            if re.search(r"related|teaser|avatar|logo", attrs.get("class") or "", re.I):
                return
            for key in ("data-srcset", "srcset"):
                candidates = []
                for item in (attrs.get(key) or "").split(","):
                    parts = item.strip().split()
                    if parts:
                        try:
                            score = float(parts[1].rstrip("wx")) if len(parts) > 1 else 0
                        except ValueError:
                            score = 0
                        candidates.append((score, parts[0]))
                self.images.extend(url for _, url in sorted(candidates, reverse=True))
            for key in ("data-original", "data-lazy-src", "data-src", "src"):
                self.images.append(attrs.get(key))


def image_urls(values, base):
    result = []
    for value in values:
        if not value:
            continue
        url = urllib.parse.urljoin(base, htmllib.unescape(value).strip())
        if urllib.parse.urlsplit(url).scheme not in ("http", "https") or JUNK.search(url):
            continue
        if url not in result:
            result.append(url)
    return result


def page_images(page_url):
    try:
        page = http(page_url, timeout=15).decode("utf-8", "ignore")
    except Exception as e:
        print(f"  ! страница новости не открылась: {type(e).__name__}")
        return []
    metadata = ImageParser()
    metadata.feed(page)
    # Без article используем только метаданные, чтобы не брать чужие превью.
    article = re.search(r"<article\b[\s\S]*?</article>", page, re.I)
    content = ImageParser()
    if article:
        content.feed(article.group(0))
    return image_urls(metadata.meta + content.images, page_url)[:12]


def download_jpeg(url, referer):
    request = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer, "Accept": "image/*"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("Изображение больше 20 МБ")
    with Image.open(io.BytesIO(raw)) as source:
        if source.width * source.height > 40_000_000:
            raise ValueError("Слишком большое разрешение изображения")
        image = ImageOps.exif_transpose(source).convert("RGBA")
        image.thumbnail((1600, 1600))
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        output = io.BytesIO()
        background.save(output, "JPEG", quality=90)
        return output.getvalue()


def photo_token():
    token = os.environ.get("VK_PHOTO_TOKEN") or os.environ.get("VK_TOKEN")
    if not token:
        raise RuntimeError("Не задан VK_TOKEN или VK_PHOTO_TOKEN")
    return token


def photo_server():
    return vk("photos.getWallUploadServer", token=photo_token(),
              group_id=GROUP_ID)["upload_url"]


def upload_photos(urls, referer="", server=None):
    if not urls:
        return []
    server = server or photo_server()
    out, uploaded = [], set()
    for i, url in enumerate(urls):
        if len(out) >= SLIDES:
            break
        try:
            img = download_jpeg(url, referer or url)
            if img in uploaded:
                continue
            body, headers = multipart("photo", f"photo{i + 1}.jpg", "image/jpeg", img)
            up = json.loads(http(server, body, headers, 60))
            if up.get("error") or not up.get("photo") or up["photo"] == "[]" or not up.get("server") or not up.get("hash"):
                raise ValueError("Сервер ВК не принял изображение")
            saved = vk("photos.saveWallPhoto", token=photo_token(), group_id=GROUP_ID,
                       server=up["server"], photo=up["photo"], hash=up["hash"])
            if not saved:
                raise ValueError("ВК не сохранил фото")
            ph = saved[0]
            attachment = f"photo{ph['owner_id']}_{ph['id']}"
            if ph.get("access_key"):
                attachment += "_" + ph["access_key"]
            out.append(attachment)
            uploaded.add(img)
        except VKError:
            raise
        except Exception as e:
            print(f"  ! кандидат фото {i + 1} не загрузился: {type(e).__name__}")
    return out


def multipart(field, filename, mime, data):
    boundary = "----stopkadr%d" % random.randint(1, 10**9)
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n")
    body = head.encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


# ---------- Главное ----------
def main():
    dry = "--dry-run" in sys.argv
    if "--check-vk" in sys.argv:
        photo_server()
        print("ВК разрешил загрузку фото. Пост не публиковался.")
        return
    # Проверяем право загрузки до расходования запроса Gemini.
    server = None if dry else photo_server()
    seen = []
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as source:
            seen = json.load(source)
    cands = fresh_news(set(seen))
    print(f"Свежих новостей: {len(cands)}")
    if not cands:
        print("Публиковать нечего — ждём следующего запуска.")
        return
    if dry:
        pick, text = cands[0], f"[пробный запуск, текст напишет Gemini]\n\n{cands[0]['title']}\n\nИсточник: {cands[0]['link']}"
    else:
        pick, text = write_post(cands)
    images = image_urls(page_images(pick["link"]) + [pick.get("image")], pick["link"])
    print("Новость:", pick["link"], "\nФото:", images, "\n---\n" + text + "\n---")
    if dry:
        return
    photos = upload_photos(images, referer=pick["link"], server=server)
    if not photos:
        raise RuntimeError("Не удалось загрузить ни одного фото. Пост НЕ опубликован; "
                           "новость не добавлена в seen.json. Проверьте источники изображений и журнал выше.")
    res = vk("wall.post", owner_id="-" + GROUP_ID, from_group=1, message=text,
             attachments=",".join(photos))
    print(f"Опубликовано: https://vk.ru/wall-{GROUP_ID}_{res['post_id']} (фото: {len(photos)})")
    seen = (seen + [pick["link"]])[-1000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
