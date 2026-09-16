import json
import os
import threading
import time
from datetime import date

import psycopg
from psycopg.rows import dict_row

CACHE_TTL = 30
_cache = {"state": None, "at": 0.0}
_lock = threading.Lock()

FONTS = {
    "modern": "'Inter', ui-sans-serif, system-ui, sans-serif",
    "serif": "Georgia, 'Times New Roman', serif",
    "rounded": "'Avenir Next Rounded', 'Nunito Sans', ui-rounded, 'Segoe UI', system-ui, sans-serif",
    "mono": "'Courier New', ui-monospace, monospace",
}

# (skin, start_month_day, end_month_day) — ranges can wrap a year boundary.
SEASON_SCHEDULE = [
    ("autumn", (9, 1), (9, 30)),
    ("halloween", (10, 1), (10, 31)),
    ("holiday", (12, 15), (1, 5)),
    ("valentine", (2, 1), (2, 14)),
    ("spring", (3, 1), (5, 31)),
    ("summer", (6, 1), (8, 31)),
]


def active_season(today=None):
    """Return the scheduled seasonal skin key active today, or '' outside a window."""
    if today is None:
        today = date.today()
    md = (today.month, today.day)
    for skin, (sm, sd), (em, ed) in SEASON_SCHEDULE:
        s, e = (sm, sd), (em, ed)
        if s <= e:
            if s <= md <= e:
                return skin
        else:
            if md >= s or md <= e:
                return skin
    return ""

PRESETS = {
    "default": {"label": "Default", "bg": "#f8fafc", "accent": "#4f46e5", "font": "modern", "emoji": "", "mood": ""},
    "autumn": {"label": "Autumn", "bg": "#fdf6ec", "accent": "#ea580c", "font": "serif", "emoji": "🍂", "mood": "leaves"},
    "holiday": {"label": "Holiday", "bg": "#f4f8ff", "accent": "#b91c1c", "font": "modern", "emoji": "🎄", "mood": "snow"},
    "valentine": {"label": "Valentine", "bg": "#fff1f2", "accent": "#db2777", "font": "rounded", "emoji": "💝", "mood": "hearts"},
    "spring": {"label": "Spring", "bg": "#f6fef4", "accent": "#16a34a", "font": "rounded", "emoji": "🌷", "mood": "petals"},
    "summer": {"label": "Summer", "bg": "#f0f9ff", "accent": "#0284c7", "font": "modern", "emoji": "🏖️", "mood": "waves"},
    "halloween": {"label": "Halloween", "bg": "#fff7ed", "accent": "#7c2d12", "font": "serif", "emoji": "🎃", "mood": "halloween"},
}

_WRAP = ('<div id="bsdec" aria-hidden="true" '
         'style="position:fixed;inset:0;z-index:40;pointer-events:none;overflow:hidden">')


def _hide_btn(chip):
    return (f'<button id="bsHideFun" type="button" '
            f'style="position:fixed;right:14px;top:12px;pointer-events:auto;z-index:41;font-size:12px;'
            f'background:rgba(255,255,255,.78);border:1px solid #e5d5c2;color:#7a5a33;border-radius:999px;'
            f'padding:4px 10px;cursor:pointer;font-family:system-ui">{chip} hide the fun</button>')


_CLOSE = ('</div><script>(function(){var h=document.getElementById("bsHideFun");'
          'if(h)h.addEventListener("click",function(){var d=document.getElementById("bsdec");if(d)d.remove()});})();</script>')


def _fall_decor(emojis, count, size_min, size_max, dur_min, dur_max, opacity=0.7, rose=False, chip="🍂"):
    dur = f"{dur_min}+Math.random()*{dur_max - dur_min}"
    anim = "bsrise linear infinite" if rose else "bsfall linear infinite"
    key = (
        "@keyframes bsfall{0%{transform:translate3d(2vw,0,0) rotate(0deg)}"
        "25%{transform:translate3d(-3vw,24vh,0) rotate(70deg)}"
        "50%{transform:translate3d(3vw,48vh,0) rotate(150deg)}"
        "75%{transform:translate3d(-2vw,72vh,0) rotate(230deg)}"
        "100%{transform:translate3d(2vw,108vh,0) rotate(340deg)}}"
        "@keyframes bsrise{0%{transform:translate3d(2vw,108vh,0) rotate(0deg)}"
        "25%{transform:translate3d(-3vw,76vh,0) rotate(-80deg)}"
        "50%{transform:translate3d(3vw,52vh,0) rotate(40deg)}"
        "75%{transform:translate3d(-2vw,28vh,0) rotate(-40deg)}"
        "100%{transform:translate3d(2vw,-12vh,0) rotate(0deg)}}"
    )
    style = (f"<style>.bsfz{{position:absolute;top:-50px;animation:{anim};will-change:transform}}"
             f"{key}</style>")
    js = (
        f"<script>(function(){{var z=document.getElementById('bsfz');if(!z)return;"
        f"var es='{emojis}'.split('');"
        f"for(var i=0;i<{count};i++){{var s=document.createElement('span');s.textContent=es[i%es.length];"
        f"s.className='bsfz';s.style.left=(Math.random()*100)+'vw';"
        f"s.style.fontSize=({size_min}+Math.random()*{size_max - size_min}).toFixed(0)+'px';"
        f"s.style.animationDuration='{dur}s';s.style.animationDelay=(-Math.random()*{dur_max}).toFixed(1)+'s';"
        f"s.style.opacity='{opacity}';z.appendChild(s);}}}})();</script>"
    )
    return _WRAP + '<div id="bsfz"></div>' + _hide_btn(chip) + _CLOSE + style + js


def _halloween_decor():
    webs = "".join(
        f'<span style="position:absolute;{pos};font-size:86px;opacity:.62" aria-hidden="true">🕸</span>'
        for pos in ["top:-8px;left:-8px", "top:-8px;right:-8px", "bottom:-8px;left:-8px", "bottom:-8px;right:-8px"]
    ) + "".join(
        f'<span style="position:absolute;{pos};font-size:{siz}px;opacity:.4" aria-hidden="true">🕸</span>'
        for pos, siz in [
            ("top:14vh;left:2vw", 46), ("top:26vh;right:2vw", 40), ("top:56vh;left:2vw", 40),
            ("bottom:24vh;right:2vw", 46), ("top:70vh;left:2vw", 48), ("bottom:44vh;left:1vw", 42),
        ]
    )
    swing = (
        '<div class="bsswing" aria-hidden="true"><div class="bsline"></div><span class="bsspider">🕷</span></div>'
        '<div class="bsswing bsswing2" aria-hidden="true"><div class="bsline"></div><span class="bsspider">🕷</span></div>'
        "<style>.bsswing{position:absolute;top:6px;left:96px;transform-origin:top center;animation:bsswing 3.4s ease-in-out infinite alternate}"
        ".bsswing2{left:auto;right:140px;animation-duration:4.2s;transform-origin:top right}"
        ".bsswing .bsline{position:absolute;left:9px;top:0;width:2px;height:70px;background:rgba(50,30,20,.45)}"
        ".bsswing2 .bsline{left:auto;right:9px}"
        ".bsswing .bsspider{position:absolute;left:0;top:70px;font-size:18px}"
        ".bsswing2 .bsspider{left:auto;right:0;font-size:20px}"
        "@keyframes bsswing{0%{transform:rotate(-16deg)}100%{transform:rotate(16deg)}}</style>"
    )
    pump = (
        '<div id="bspump" aria-hidden="true"></div>'
        "<style>#bspump span{position:absolute;bottom:-40px;animation:bsbob ease-in-out infinite alternate;will-change:transform;opacity:.68}"
        "@keyframes bsbob{0%{transform:translateY(0) rotate(-5deg)}100%{transform:translateY(-48px) rotate(6deg)}}</style>"
        "<script>(function(){var z=document.getElementById('bspump');if(!z)return;"
        "for(var i=0;i<9;i++){var s=document.createElement('span');s.textContent='🎃';"
        "s.style.left=((2+i*11)+Math.random()*7)+'vw';s.style.fontSize=(30+Math.random()*24).toFixed(0)+'px';"
        "s.style.animationDuration=(2.2+Math.random()*1.8).toFixed(2)+'s';z.appendChild(s);}})();</script>"
    )
    crawlers = (
        '<div id="bscrawl" aria-hidden="true"></div>'
        "<style>#bscrawl span{position:absolute;font-size:20px;opacity:.9;will-change:left,top}"
        ".bscspin{animation:bsspin .5s steps(2) infinite}@keyframes bsspin{0%{transform:rotate(-14deg)}100%{transform:rotate(14deg)}}</style>"
        "<script>(function(){var z=document.getElementById('bscrawl');if(!z)return;"
        "var n=7;var px=[];for(var i=0;i<n;i++){var s=document.createElement('span');s.textContent='🕷';s.className='bscspin';z.appendChild(s);"
        "var l=(12+i*60)%80;px.push({s:s,edges:[[0,l],[1,14+i*18%40],[2,l+24],[3,20+i*25%45]],seg:i%4,pos:Math.random()*40});}"
        "function W(){return window.innerWidth}function H(){return window.innerHeight}"
        "var last=0;function step(t){var dt=Math.min(0.06,(t-last)/1000||0.016);last=t;"
        "for(var i=0;i<px.length;i++){var a=px[i];var seg=a.edges[a.seg]||[0,0],e=seg[0],x=0,y=0;"
        "if(e===0){x=a.pos;y=8}else if(e===1){x=W()-8;y=a.pos}"
        "if(e===2){x=W()-a.pos;y=H()-8}else if(e===3){x=8;y=H()-a.pos}"
        "a.pos+=72*dt;if(a.pos>seg[1]){a.pos=0;a.seg=(a.seg+1)%4}"
        "a.s.style.left=x+'px';a.s.style.top=y+'px';}"
        "requestAnimationFrame(step);}requestAnimationFrame(step);})();</script>"
    )
    graveyard = (
        '<div id="bsgrave" aria-hidden="true"></div><div id="bsbats" aria-hidden="true"></div>'
        '<div class="bsmoon" aria-hidden="true">🌕</div><div class="bsghost" aria-hidden="true">👻</div>'
        '<div class="bsghost bsghost2" aria-hidden="true">👻</div><div class="bsfog" aria-hidden="true">🌫️</div>'
        "<style>.bsmoon{position:absolute;top:16px;left:50%;margin-left:-30px;font-size:60px;opacity:.9;"
        "animation:bsmoonin 3s ease-out;filter:drop-shadow(0 0 18px rgba(251,191,36,.6))}"
        "@keyframes bsmoonin{from{opacity:0}to{opacity:.9}}"
        ".bsghost{position:absolute;bottom:46px;left:6vw;font-size:34px;opacity:.7;animation:bsghost 9s ease-in-out infinite alternate}"
        ".bsghost2{left:auto;right:10vw;bottom:70px;font-size:26px;animation-duration:13s;animation-delay:-4s}"
        "@keyframes bsghost{0%{transform:translate(0,0)}100%{transform:translate(26vw,-40px)}}"
        "#bsbats span{position:absolute;top:0;font-size:22px;opacity:.7;animation:bsfly linear infinite}"
        "@keyframes bsfly{0%{transform:translateX(108vw) translateY(0) rotate(6deg)}"
        "50%{transform:translateX(54vw) translateY(-30px) rotate(-8deg)}100%{transform:translateX(-14vw) translateY(0) rotate(6deg)}}"
        "#bsgrave span{position:absolute;bottom:0;font-size:38px;opacity:.85;animation:bsg 3s ease-in-out infinite alternate}"
        "@keyframes bsg{0%{transform:translateY(0) rotate(-2deg)}100%{transform:translateY(-3px) rotate(2deg)}}"
        ".bsfog{position:absolute;bottom:-10px;left:-12vw;font-size:70px;opacity:.16;filter:blur(10px);animation:bsfog 22s ease-in-out infinite alternate}"
        "@keyframes bsfog{0%{transform:translateX(0) }100%{transform:translateX(84vw)}}</style>"
        "<script>(function(){var g=document.getElementById('bsgrave');if(g){"
        "for(var i=0;i<9;i++){var s=document.createElement('span');s.textContent='🪦';"
        "s.style.left=(1+i*12+Math.random()*3)+'vw';s.style.animationDelay=(-Math.random()*3).toFixed(1)+'s';g.appendChild(s);}}"
        "var b=document.getElementById('bsbats');if(b){"
        "for(var j=0;j<5;j++){var t=document.createElement('span');t.textContent='🦇';"
        "t.style.top=(8+j*18)+'vh';t.style.animationDuration=(12+Math.random()*10).toFixed(1)+'s';"
        "t.style.animationDelay=(-Math.random()*24).toFixed(1)+'s';b.appendChild(t);}}})();</script>"
    )
    return (
        _WRAP + pump + webs + swing + crawlers + graveyard + _hide_btn("🎃") + "</div>" + _CLOSE
    )


def _waves_decor():
    return (
        _WRAP
        + '<div style="position:absolute;top:20px;right:24px;font-size:34px;animation:bssun 4s ease-in-out infinite alternate" aria-hidden="true">☀️</div>'
        + '<div style="position:absolute;right:20px;bottom:64px;font-size:30px;opacity:.9;transform:rotate(-2deg)" aria-hidden="true">🏖️</div>'
        + '<div class="bsw bsw1" aria-hidden="true"></div><div class="bsw bsw2" aria-hidden="true"></div>'
        + "<style>.bsw{position:absolute;left:0;right:0;bottom:0;height:110px;background-size:200px 90px;background-repeat:repeat-x;"
        "background-image:url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 90'>"
        "<path d='M0 90 Q 25 40 50 60 T 100 60 T 150 60 T 200 60 L 200 90 Z' fill='%2338bdf8'/></svg>\");animation:bswavel linear infinite}"
        ".bsw1{animation-duration:18s;opacity:.55}.bsw2{animation-duration:30s;opacity:.85;bottom:-6px;height:70px;background-size:160px 60px}"
        "@keyframes bswavel{0%{background-position-x:0}100%{background-position-x:-200px}}"
        "@keyframes bssun{0%{transform:translateY(0) rotate(-6deg)}100%{transform:translateY(-10px) rotate(6deg)}}</style>"
        + _hide_btn("🏖️") + "</div>" + _CLOSE
    )


def _build_decor(mood):
    return {
        "leaves": lambda: _fall_decor("🍂🍁", 12, 18, 32, 8, 16),
        "snow": lambda: _fall_decor("❄️", 14, 12, 24, 8, 16, chip="❄️"),
        "hearts": lambda: _fall_decor("💗💕", 10, 16, 28, 8, 15, rose=True, chip="💗"),
        "petals": lambda: _fall_decor("🌸🌷", 12, 16, 26, 8, 15, chip="🌷"),
        "halloween": _halloween_decor,
        "waves": _waves_decor,
    }.get(mood, lambda: "")()


def _db():
    url = os.getenv(
        "DATABASE_URL", "postgresql://shaun:secret@localhost:5432/bizstack"
    )
    conn = psycopg.connect(url, row_factory=dict_row)
    conn.autocommit = True
    return conn


def _read_settings(conn):
    out = {}
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM app_settings WHERE key = ANY(%s);", (["theme", "promo_first_clean", "stripe_coupon_first_clean"],))
        for r in cur.fetchall():
            out[r["key"]] = r["value"]
    return out


def state(force=False):
    now = time.time()
    with _lock:
        if not force and _cache["state"] and (now - _cache["at"]) < CACHE_TTL:
            return _cache["state"]
        try:
            conn = _db()
        except Exception:
            cached = _cache["state"]
            if cached:
                return cached
            return {"skin": "default", "css": "", "emoji": "", "decor": "", "promo_on": False, "auto": True, "auto_season": ""}
        try:
            settings = _read_settings(conn)
            theme = {}
            try:
                theme = json.loads(settings.get("theme") or "{}")
            except (TypeError, ValueError):
                theme = {}
            auto = bool(theme.get("auto", True))
            if auto:
                # Seasonal schedule drives the public look automatically.
                season_now = active_season()
                skin_name = season_now or "default"
                base = dict(PRESETS.get(skin_name, PRESETS["default"]))
            else:
                season_now = ""
                skin_name = str(theme.get("skin") or "default")
                base = dict(PRESETS.get(skin_name, PRESETS["default"]))
                if "bg" in theme:
                    base["bg"] = theme["bg"]
                if "accent" in theme:
                    base["accent"] = theme["accent"]
                if "font" in theme:
                    base["font"] = str(theme["font"])
                if "emoji" in theme and theme.get("emoji") is not None:
                    base["emoji"] = str(theme["emoji"])
            state = {
                "skin": skin_name,
                "label": base.get("label") or skin_name,
                "bg": base["bg"],
                "accent": base["accent"],
                "font_name": base["font"],
                "emoji": base.get("emoji") or "",
                "promo_on": settings.get("promo_first_clean") == "on",
                "decor": _build_decor(base.get("mood") or ""),
                "css": css(base["bg"], base["accent"], base["font"]),
                "auto": auto,
                "auto_season": season_now,
            }
            _cache["state"] = state
            _cache["at"] = now
        finally:
            conn.close()
        return _cache["state"]


def save_theme(db, skin=None, bg=None, accent=None, font=None, emoji=None, auto=None):
    with db.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = 'theme';")
        row = cur.fetchone()
        theme = {}
        try:
            theme = json.loads((row or {}).get("value") or "{}")
        except (TypeError, ValueError):
            theme = {}
        if auto is not None:
            theme["auto"] = bool(auto)
        if not theme.get("auto", True):
            if skin is not None:
                theme["skin"] = skin
            if bg is not None:
                theme["bg"] = bg
            if accent is not None:
                theme["accent"] = accent
            if font is not None:
                theme["font"] = font
            if emoji is not None:
                theme["emoji"] = emoji
        cur.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('theme', %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();",
            (json.dumps(theme),),
        )
        db.commit()
    state(force=True)


def set_promo(db, value):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('promo_first_clean', %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();",
            ("on" if value else "off",),
        )
        db.commit()
    state(force=True)


def css(bg, accent, font):
    stack = FONTS.get(str(font), FONTS["modern"])
    return f"""<style>
:root{{--bs-bg:{bg};--bs-accent:{accent};--bs-font:{stack};}}
body{{background-color:var(--bs-bg)!important;font-family:var(--bs-font)!important;}}
.bg-indigo-600,.bg-indigo-700,button[type=submit].bg-indigo-600{{background-color:var(--bs-accent)!important;}}
.hover\\:bg-indigo-700:hover,.hover\\:bg-indigo-600:hover{{background-color:color-mix(in srgb,var(--bs-accent) 85%,#000)!important;}}
.bg-indigo-50,.bg-indigo-100{{background-color:color-mix(in srgb,var(--bs-accent) 10%,#fff)!important;}}
.text-indigo-600,.text-indigo-700,.hover\\:text-indigo-600:hover,.hover\\:text-indigo-700:hover{{color:var(--bs-accent)!important;}}
.ring-indigo-500:focus{{--tw-ring-color:var(--bs-accent)!important;}}
.shadow-indigo-100{{box-shadow:0 4px 6px -1px color-mix(in srgb,var(--bs-accent) 12%,transparent)!important;}}
.shadow-indigo-200{{box-shadow:0 10px 15px -3px color-mix(in srgb,var(--bs-accent) 15%,transparent)!important;}}
</style>"""