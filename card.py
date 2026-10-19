"""Инфографика-карточка для Telegram (скилл graphic-designer, путь A: HTML/CSS -> PNG).

Карточка: кадр из новости сверху, заголовок, плашки-теги, таблица «иконка — описание» и подпись канала.
HTML рендерится headless-браузером (Chrome/Chromium/Edge) в PNG, лишний фон снизу обрезается.
"""
import base64
import html
import io
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

WIDTH = 1080
BG = (15, 17, 23)            # #0f1117 — фон страницы, по нему обрезаем низ
ACCENT = '#ff7a3d'           # фирменный оранжевый СтопКадра
ALLOWED = ('b', 'i')


def browser():
    """Путь к headless-браузеру: переменная CHROME или стандартные места."""
    candidates = [os.environ.get('CHROME', ''), 'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser',
                  r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                  r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe']
    for c in candidates:
        if c and (shutil.which(c) or Path(c).exists()):
            return shutil.which(c) or c
    return None


def safe(text):
    """Экранирует текст, оставляя только <b> и <i>."""
    out = html.escape(html.unescape(text or ''), quote=False)
    return re.sub(r'&lt;(/?)(%s)&gt;' % '|'.join(ALLOWED), r'<\1\2>', out)


def card_html(data, hero=None):
    hero_css = ''
    if hero:
        hero_css = f"background-image:url('data:image/jpeg;base64,{base64.b64encode(hero).decode()}');"
    tags = ''.join(f'<span class="tag t{i % 3}">{safe(t)}</span>' for i, t in enumerate(data.get('tags', [])[:3]))
    rows = ''.join(
        f'<div class="row"><div class="ico">{safe(r.get("icon", "")) or i + 1}</div>'
        f'<div class="txt">{safe(r.get("text", ""))}</div></div>'
        for i, r in enumerate(data.get('rows', [])[:5]))
    return f'''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width={WIDTH}">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=block" rel="stylesheet">
<style>
  html,body{{margin:0;background:rgb{BG};}}
  body{{width:{WIDTH}px;font-family:Inter,'Segoe UI','DejaVu Sans',system-ui,sans-serif;color:#e8eaf0;}}
  .card{{padding:48px 48px 40px;}}
  .hero{{height:440px;border-radius:28px;background:#1b1f2a center/cover no-repeat;{hero_css}position:relative;overflow:hidden;}}
  .hero:after{{content:'';position:absolute;inset:0;background:linear-gradient(180deg,rgba(15,17,23,0) 35%,rgba(15,17,23,.92) 100%);}}
  .title{{position:absolute;left:40px;right:40px;bottom:34px;z-index:2;font-size:62px;line-height:1.05;font-weight:800;
         letter-spacing:-1px;color:#fff;text-shadow:0 4px 24px rgba(0,0,0,.6);}}
  .tags{{display:flex;flex-wrap:wrap;gap:14px;margin:28px 0 30px;}}
  .tag{{font-size:26px;font-weight:800;text-transform:uppercase;letter-spacing:1px;padding:10px 20px;border-radius:12px;color:#fff;}}
  .t0{{background:{ACCENT};}} .t1{{background:#6c4cf5;}} .t2{{background:#2a7fff;}}
  .table{{border:2px solid #2a2f3c;border-radius:24px;overflow:hidden;background:#161a24;}}
  .row{{display:flex;align-items:stretch;border-top:2px solid #2a2f3c;}}
  .row:first-child{{border-top:none;}}
  .ico{{flex:0 0 132px;display:flex;align-items:center;justify-content:center;font-size:60px;background:#1d2230;
        border-right:2px solid #2a2f3c;font-weight:800;color:{ACCENT};}}
  .txt{{padding:28px 32px;font-size:31px;line-height:1.38;}}
  .txt b{{color:#fff;font-weight:800;}}
  .foot{{display:flex;justify-content:space-between;align-items:center;margin-top:30px;font-size:26px;color:#8a90a2;}}
  .brand{{font-weight:800;color:#fff;letter-spacing:2px;}} .brand span{{color:{ACCENT};}}
</style></head><body><div class="card">
  <div class="hero"><div class="title">{safe(data.get("title", ""))}</div></div>
  <div class="tags">{tags}</div>
  <div class="table">{rows}</div>
  <div class="foot"><div class="brand">СТОП<span>КАДР</span></div><div>кино · сериалы · игры</div></div>
</div></body></html>'''


def crop_bottom(png, pad=8):
    """Обрезает однотонный фон снизу (окно рендерим с запасом по высоте)."""
    with Image.open(io.BytesIO(png)) as im:
        im = im.convert('RGB')
        w, h = im.size
        bottom = h
        px = im.load()
        for y in range(h - 1, 0, -1):
            if any(abs(c - b) > 6 for x in range(0, w, 9) for c, b in zip(px[x, y], BG)):
                bottom = min(h, y + 40 + pad)
                break
        out = io.BytesIO()
        im.crop((0, 0, w, bottom)).save(out, 'JPEG', quality=95)
        return out.getvalue()


def render_card(data, hero=None, height=2600, timeout=60):
    """Рисует карточку и возвращает JPEG (bytes) или None, если браузера нет или рендер не удался."""
    exe = browser()
    if not exe or not data.get('rows'):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / 'card.html'
        shot = Path(tmp) / 'card.png'
        page.write_text(card_html(data, hero), encoding='utf-8')
        cmd = [exe, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-sandbox', '--force-device-scale-factor=1',
               f'--window-size={WIDTH},{height}', '--virtual-time-budget=10000', f'--screenshot={shot}', page.as_uri()]
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout)
        except Exception as exc:
            print('Карточка: браузер не отработал:', type(exc).__name__)
            return None
        if not shot.exists():
            print('Карточка: браузер не сохранил скриншот')
            return None
        return crop_bottom(shot.read_bytes())
