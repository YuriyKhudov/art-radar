"""Art Radar — хаб для художника: видео, новости, ярмарки, галереи, форматы.

Запускает маленький сервер на этом компьютере (только для себя, http://127.0.0.1:8765) и открывает
браузер. Страница сразу показывает то, что уже собрано, а свежие видео и статьи подгружает через
несколько секунд. Что вы уже посмотрели, запоминает браузер.

Запуск: python -m radar.app            (python -m radar.app --export — сохранить страницу файлом)
"""

import json
import socket
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from radar.news import collect_news, recent_articles
from radar import sales
from radar.notify import ROOT, SEED_FILE, all_channels, channels_to_watch, collect, db, load_settings, log

PAGE_FILE = ROOT / "data" / "Art_Radar.html"
EXAMPLE_FILE = ROOT / "design" / "mockup_v2.html"  # пример раздела «Что покупают» с выдуманными цифрами
PORT = 8765
URL = f"http://127.0.0.1:{PORT}/"
IDLE_EXIT_SECONDS = 2 * 60 * 60  # без открытой страницы сервер сам закрывается через 2 часа


def recent_videos(settings, limit=400):
    info = {c["channel_id"]: c for c in all_channels()}
    watched = [c["channel_id"] for c in channels_to_watch(settings)]
    rows = db().execute(
        f"SELECT video_id, channel_id, title, url, published_at, notified_at FROM seen_videos "
        f"WHERE channel_id IN ({','.join('?' * len(watched))}) ORDER BY published_at DESC LIMIT ?",
        [*watched, limit]).fetchall()
    return [{
        "id": vid, "channel": info.get(ch, {}).get("name", ch), "channel_id": ch,
        "category": info.get(ch, {}).get("category", ""), "title": title, "url": url,
        "published": published,
        # Видео, которые уже были на канале при подключении, сразу считаются просмотренными.
        "initial": notified == "skipped-initial",
    } for vid, ch, title, url, published, notified in rows]


def page_data(settings):
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    try:
        articles = recent_articles()
    except Exception:  # таблицы статей ещё нет — до первого обновления
        articles = []
    return {
        "videos": recent_videos(settings),
        "articles": articles,
        "fairs": seed.get("fairs", []),
        "galleries": seed.get("galleries", []),
        "sales": sales.stats_records(),
        "sales_own": sales.all_records("entry_method NOT LIKE 'dataset%'"),
        "datasets": [sales.LEE2024],
    }


def render_page(settings):
    payload = json.dumps(page_data(settings), ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("/*DATA*/{}", payload)


def refresh(settings):
    """Проверить каналы и издания одновременно; ошибки одного источника не мешают остальным."""
    def safe(step):
        try:
            step()
        except Exception as e:  # нет интернета и т. п. — показываем то, что уже есть
            log(f"Не удалось обновить: {e}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(safe, [lambda: collect(settings), collect_news]))


def port_busy():
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def serve(settings):
    lock = threading.Lock()
    last_seen = [time.time()]

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, ctype):
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            last_seen[0] = time.time()
            if self.path == "/":
                self._send(render_page(settings), "text/html")
            elif self.path == "/api/refresh":
                with lock:  # одно обновление за раз, даже если открыто несколько вкладок
                    refresh(settings)
                self._send(json.dumps(page_data(settings), ensure_ascii=False), "application/json")
            elif self.path == "/example":
                self._send(EXAMPLE_FILE.read_text(encoding="utf-8"), "text/html")
            elif self.path == "/api/ping":
                self._send("ok", "text/plain")
            else:
                self.send_error(404)

        def do_POST(self):
            last_seen[0] = time.time()
            # Особый заголовок: чужие сайты в браузере не могут его отправить (защита от подделки запросов).
            if self.headers.get("X-Radar") != "1":
                self.send_error(403)
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            try:
                if self.path == "/api/sales/parse":
                    result = sales.parse_lot(body.get("text", ""))
                elif self.path == "/api/sales/save":
                    result = {"id": sales.save(body, entry_method=body.get("entry_method", "manual"))}
                elif self.path == "/api/sales/delete":
                    sales.delete(body["id"])
                    result = {}
                elif self.path == "/api/sales/import":
                    result = sales.import_csv(body.get("text", ""))
                else:
                    self.send_error(404)
                    return
                result["sales"] = sales.stats_records()
                result["sales_own"] = sales.all_records("entry_method NOT LIKE 'dataset%'")
            except sales.SaleError as e:
                result = {"error": str(e)}
            self._send(json.dumps(result, ensure_ascii=False), "application/json")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def watchdog():
        while time.time() - last_seen[0] < IDLE_EXIT_SECONDS:
            time.sleep(60)
        server.shutdown()
    threading.Thread(target=watchdog, daemon=True).start()
    server.serve_forever()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = load_settings()
    if "--export" in sys.argv:
        refresh(settings)
        PAGE_FILE.parent.mkdir(exist_ok=True)
        PAGE_FILE.write_text(render_page(settings), encoding="utf-8")
        print(PAGE_FILE)
        return
    if port_busy():  # программа уже запущена — просто открываем страницу
        webbrowser.open(URL)
        return
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(URL)).start()
    serve(settings)


TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Art Radar</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#f7f3ec;--paper:#fffdf8;--ink:#332d29;--muted:#71665d;--accent:#a34e36;--soft:#f0dfd4;--line:#ded5c9;--side:#efe8dd;--on-accent:#fff;--shadow:0 8px 26px #4834271a;--canvas:#efe6d8;--canvas-edge:#cdbfae}
@media (prefers-color-scheme:dark){:root{--bg:#24211e;--paper:#302b26;--ink:#f5eee3;--muted:#c4b7a9;--accent:#efa78a;--soft:#49352c;--line:#54483e;--side:#2a2622;--on-accent:#24211e;--shadow:0 8px 26px #0004;--canvas:#d8cab6;--canvas-edge:#8a7a66}}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);font:17px/1.55 Manrope,system-ui,sans-serif}
a{color:var(--accent)}
.app{display:grid;grid-template-columns:272px 1fr;min-height:100vh}
/* --- боковая панель, как список папок в Telegram --- */
.side{background:var(--side);border-right:1px solid var(--line);padding:22px 12px;position:sticky;top:0;height:100vh;overflow:auto}
.logo{font:600 30px/1 "Cormorant Garamond",Georgia,serif;padding:4px 12px 20px}
.nav{display:grid;gap:4px}
.nav a{display:flex;align-items:center;gap:12px;padding:12px;border-radius:14px;color:var(--ink);text-decoration:none;font-weight:600}
.nav a:hover{background:var(--soft)}
.nav a.on{background:var(--accent);color:var(--on-accent)}
.nav .ic{font-size:22px;width:28px;text-align:center}
.nav .lbl{flex:1}
.nav .badge{background:var(--accent);color:var(--on-accent);border-radius:999px;min-width:26px;height:26px;padding:0 8px;display:inline-grid;place-items:center;font-size:13px;font-weight:700}
.nav a.on .badge{background:#fff;color:var(--accent)}
.nav .hint{font-size:12px;font-weight:600;color:var(--muted)}
.nav a.on .hint{color:var(--on-accent)}
/* --- содержимое --- */
main{min-width:0}
.wrap{max-width:900px;margin:0 auto;padding:28px 20px 100px}
h1{font:600 40px/1.1 "Cormorant Garamond",Georgia,serif;margin:0 0 6px}
h2{font:600 28px/1.2 "Cormorant Garamond",Georgia,serif;margin:36px 0 14px}
h3{margin:0 0 6px;font-size:19px}
.sub{color:var(--muted);margin:0 0 22px}
.bar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin:4px 0 14px}
.count{display:inline-flex;align-items:center;gap:10px;font-weight:600;font-size:19px}
.pill{background:var(--accent);color:var(--on-accent);border-radius:999px;min-width:34px;height:34px;padding:0 10px;display:inline-grid;place-items:center;font-size:16px}
button,.btn{font:600 16px Manrope,sans-serif;border-radius:14px;min-height:48px;padding:0 20px;cursor:pointer;border:1px solid var(--line);background:var(--paper);color:var(--ink);text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:8px}
.btn.main{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
button:hover,.btn:hover{filter:brightness(.97)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 20px}
.chip{min-height:42px;padding:0 16px;border-radius:999px;font-size:15px}
.chip.on{background:var(--ink);border-color:var(--ink);color:var(--paper)}
.chip small{opacity:.75;font-weight:700;margin-left:4px}
.list{display:grid;gap:16px}
.list .more{grid-column:1/-1;width:100%}
.card{background:var(--paper);border:1px solid var(--line);border-radius:18px;padding:16px;box-shadow:var(--shadow)}
.card.new{border-color:var(--accent);box-shadow:0 0 0 2px var(--soft),var(--shadow)}
.vcard{display:grid;grid-template-columns:220px 1fr;gap:18px}
.thumb{position:relative;display:block;border-radius:12px;overflow:hidden;aspect-ratio:16/9;background:var(--soft)}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.newtag{display:inline-block;background:var(--accent);color:var(--on-accent);font-size:12px;font-weight:700;padding:2px 10px;border-radius:999px;margin-right:6px;vertical-align:2px}
.thumb .newtag{position:absolute;top:8px;left:8px}
.meta{color:var(--muted);font-size:15px;margin-bottom:4px}
.title{font-weight:600;font-size:18px;margin:0 0 10px}
.title a{color:inherit;text-decoration:none}
.text{color:var(--muted);margin:0 0 14px;font-size:16px}
.actions{display:flex;flex-wrap:wrap;gap:10px}
.empty{background:var(--paper);border:1px dashed var(--line);border-radius:18px;padding:28px;text-align:center;color:var(--muted)}
.empty b{display:block;color:var(--ink);font-size:20px;margin-bottom:6px}
details summary{cursor:pointer;color:var(--accent);font-weight:600;margin:30px 0 14px;list-style:none}
details summary::-webkit-details-marker{display:none}
.note{font-size:14px;color:var(--muted);margin-top:36px}
/* ярмарки */
.fair{display:grid;grid-template-columns:96px 1fr auto;gap:16px;align-items:center}
.date{background:var(--soft);border-radius:14px;text-align:center;padding:10px 6px;font-weight:700;line-height:1.2}
.date b{display:block;font:600 30px/1 "Cormorant Garamond",serif}
.soon{font-weight:700;color:var(--accent);white-space:nowrap}
.fair.past{opacity:.6}
/* галерея */
.links{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0 0}
.facts{display:grid;gap:8px;margin:0;padding:0;list-style:none}
.facts li::before{content:"• ";color:var(--accent)}
/* мой размер */
.sizebox{display:grid;grid-template-columns:1fr 1fr;gap:22px}
.field label{display:block;font-weight:600;margin-bottom:6px}
.field input{width:100%;font:600 22px Manrope,sans-serif;padding:10px 14px;border-radius:14px;border:1px solid var(--line);background:var(--paper);color:var(--ink)}
.inputs{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.units{display:flex;gap:8px;margin-bottom:14px}
.quick{display:flex;flex-wrap:wrap;gap:8px}
.quick button{min-height:40px;padding:0 14px;font-size:15px}
.stage{background:var(--soft);border-radius:18px;min-height:300px;display:grid;place-items:center;padding:24px}
.canvas{background:var(--canvas);box-shadow:6px 8px 0 var(--canvas-edge),0 14px 30px #0002;border-radius:3px;transition:all .25s;background-image:repeating-linear-gradient(45deg,#0000 0 3px,#0000000a 3px 4px)}
.answers{display:grid;gap:10px;margin-top:18px}
.answers div{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:12px 16px}
.answers b{display:block}
.pinned{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px;margin:0 0 16px}
.pin{display:flex;align-items:center;gap:12px;background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:12px 14px;cursor:pointer;box-shadow:var(--shadow)}
.pin:hover{border-color:var(--accent)}
.pin.on{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent)}
.pinic{font-size:20px}
.pinname{flex:1;min-width:0;line-height:1.3}
.pinname b{display:block}
.pinname span{font-size:13px;color:var(--muted)}
.pin .badge{background:var(--accent);color:var(--on-accent);border-radius:999px;min-width:26px;height:26px;padding:0 8px;display:inline-grid;place-items:center;font-size:13px;font-weight:700}
.pin a{font-size:13px;font-weight:600;white-space:nowrap}
.list.shorts{grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}
.scard{padding:10px;display:flex;flex-direction:column}
.scard .thumb{aspect-ratio:9/16;margin-bottom:10px}
.scard .title{font-size:15px;flex:1}
.scard .meta{font-size:13px}
.scard .mark{min-height:40px;font-size:14px}
.sizes{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px}
.card.size{display:flex;flex-direction:column;gap:2px;text-align:center;align-items:center}
.easelbox{padding:6px!important}
.easelbox svg{width:100%;height:auto;max-width:260px;display:block;margin:auto}
.easelbox.big svg{max-width:460px}
.stage.small{min-height:150px;width:100%;padding:12px;margin-bottom:8px}
.big{font-size:19px}
.techrow,.salerow{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;justify-content:space-between}
.del{min-height:40px;font-size:14px}
dialog{border:0;border-radius:20px;padding:0;max-width:720px;width:calc(100% - 24px);background:var(--paper);color:var(--ink);box-shadow:0 30px 80px #0006}
dialog::backdrop{background:#0008}
.dlg{padding:24px;display:grid;gap:12px}
.dlg label{display:grid;gap:4px;font-weight:600;font-size:15px}
.dlg input,.dlg select,.dlg textarea{font:16px Manrope,sans-serif;padding:10px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
.dlg .lbl{font-weight:700;font-size:17px;margin-top:6px}
.dlg .check{display:flex;gap:10px;align-items:flex-start;font-weight:600}
.dlg .check input{width:22px;height:22px;margin-top:2px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.warn{color:var(--accent);margin:4px 0}.ok{color:#6fa36a;margin:4px 0}.err{color:#d9534f;font-weight:700}
@media (max-width:760px){.grid2{grid-template-columns:1fr}}
.cats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.cat{flex-direction:column;align-items:flex-start;gap:4px;padding:16px;min-height:120px;text-align:left;border-radius:18px}
.cat:disabled{opacity:.55;cursor:default}
.cat.on{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent)}
.catic{font-size:28px}
.cat b{font-size:18px}
.cat span:last-child{font-weight:500;color:var(--muted);font-size:15px}
.datablock{margin-top:28px}
.status{font-size:13px;color:var(--muted);padding:18px 12px 0}
.status-m{display:none}
.status.busy::before{content:"";display:inline-block;width:10px;height:10px;margin-right:8px;border-radius:50%;border:2px solid var(--accent);border-right-color:transparent;animation:spin .8s linear infinite;vertical-align:-1px}
@keyframes spin{to{transform:rotate(360deg)}}
/* нижняя панель на телефоне */
@media (max-width:760px){
  .side .status{display:none}
  .status-m{display:block;font-size:12px;color:var(--muted);margin:0 0 6px}
  .status-m:empty{display:none}
  .status-m.busy::before{content:"";display:inline-block;width:9px;height:9px;margin-right:6px;border-radius:50%;border:2px solid var(--accent);border-right-color:transparent;animation:spin .8s linear infinite;vertical-align:-1px}
  .wrap{padding:14px 14px calc(96px + env(safe-area-inset-bottom))}
  h1{font-size:32px}
  h2{font-size:23px;margin:26px 0 10px}
  .sub{font-size:15px;margin-bottom:14px}
  /* кнопки тем — одной строкой с прокруткой пальцем */
  .chips{flex-wrap:nowrap;overflow-x:auto;margin:0 -14px 14px;padding:0 14px 4px;scrollbar-width:none;-webkit-overflow-scrolling:touch}
  .chips::-webkit-scrollbar{display:none}
  .chip{flex:none;min-height:38px;padding:0 14px;font-size:14px}
  /* закреплённые каналы — компактно, в две колонки */
  .pinned{grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}
  .pin{padding:10px 12px;gap:6px;flex-wrap:wrap;align-items:flex-start}
  .pinic,.pinname span{display:none}
  .pinname{flex:1 1 100%}
  .pinname b{font-size:14px}
  .pin a{font-size:12px;margin-left:auto}
  .bar{flex-wrap:nowrap;gap:8px}
  .bar .count{font-size:17px;white-space:nowrap}
  #allSeen{min-height:38px;font-size:13px;padding:0 12px}
  .card{padding:12px;border-radius:16px}
  .vcard{gap:10px}
  .title{font-size:16px;margin-bottom:10px}
  .actions .btn,.actions button{min-height:44px;font-size:14px;padding:0 12px}
  .list.shorts{grid-template-columns:1fr 1fr;gap:10px}
  /* «Что покупают»: плитки в три колонки, форматы по два в ряд */
  .cats{grid-template-columns:repeat(3,1fr);gap:8px}
  .cat{min-height:0;padding:10px;border-radius:14px}
  .catic{font-size:22px}
  .cat b{font-size:14px}
  .cat span:last-child{font-size:11px;line-height:1.35}
  .sizes{grid-template-columns:1fr 1fr;gap:10px}
  .card.size{padding:8px}
  .card.size span{font-size:12px;line-height:1.35}
  .stage.small{min-height:0;padding:2px}
  .big{font-size:16px}
  .stage{min-height:220px;padding:12px}
  .fair{gap:12px}
  .app{grid-template-columns:1fr}
  .side{position:fixed;bottom:0;top:auto;left:0;right:0;height:auto;padding:6px 4px calc(6px + env(safe-area-inset-bottom));z-index:5;border-right:0;border-top:1px solid var(--line)}
  .logo,.nav .hint{display:none}
  .nav{grid-template-columns:repeat(5,1fr);gap:2px}
  .nav a{flex-direction:column;gap:2px;padding:6px 2px;font-size:11px;position:relative;text-align:center}
  .nav .ic{font-size:22px}
  .nav .badge{position:absolute;top:2px;right:calc(50% - 26px);min-width:20px;height:20px;font-size:11px;padding:0 5px}
  .vcard,.sizebox{grid-template-columns:1fr}
  .fair{grid-template-columns:76px 1fr}
  .fair .soon{grid-column:2}
  h1{font-size:34px}
  .actions .btn,.actions button{flex:1}
}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="logo">Art Radar</div>
    <nav class="nav" id="nav"></nav>
    <div class="status" id="status"></div>
  </aside>
  <main><div class="wrap"><div class="status-m" id="status2"></div><div id="view"></div></div></main>
</div>

<script>
const DATA = /*DATA*/{};
const SECTIONS = [
  {id:"videos", icon:"🎬", label:"Видео"},
  {id:"shorts", icon:"📱", label:"Шортсы"},
  {id:"news", icon:"📰", label:"Новости"},
  {id:"fairs", icon:"🗓", label:"Ярмарки"},
  {id:"formats", icon:"📐", label:"Форматы"},
];
const TOPIC_NAMES = {galleries:"Галереи", exhibitions:"Выставки", fairs:"Ярмарки", artists:"Художники", market:"Рынок", news:"Новости"};

// ---------- что уже просмотрено (хранится в браузере) ----------
const store = {
  get(k, d){ try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch (e) { return d; } },
  set(k, v){ try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
};
const seen = new Set(store.get("artradar-seen", []));
const saveSeen = () => store.set("artradar-seen", [...seen]);
let since = store.get("artradar-since", 0);
if (!since) { since = Date.now() - 7 * 864e5; store.set("artradar-since", since); }
const isSeen = x => x.initial || seen.has(x.id) || (Date.parse(x.published) || 0) < since;
const markSeen = ids => { ids.forEach(i => seen.add(i)); saveSeen(); };

const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function when(iso){
  const d = new Date(iso), now = new Date();
  if (isNaN(d)) return "";
  const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 864e5);
  const time = d.toLocaleTimeString("ru-RU", {hour:"2-digit", minute:"2-digit"});
  if (days === 0) return "сегодня в " + time;
  if (days === 1) return "вчера в " + time;
  return d.toLocaleDateString("ru-RU", {day:"numeric", month:"long"});
}
function greet(){
  const h = new Date().getHours();
  return h < 6 ? "Доброй ночи" : h < 12 ? "Доброе утро" : h < 18 ? "Добрый день" : "Добрый вечер";
}
const daysTo = s => Math.ceil((new Date(s + "T00:00:00") - new Date(new Date().toDateString())) / 864e5);

// ---------- меню ----------
let current = (location.hash.slice(1) || store.get("artradar-section", "videos"));
if (!SECTIONS.some(s => s.id === current)) current = "videos";
const filters = {videos:"", shorts:"", news:""};
const PAGE = 40, limits = {};  // сколько карточек показано в ленте; растёт по кнопке «Показать ещё»
// Шортсы — короткие вертикальные ролики; YouTube отдаёт их по ссылке вида /shorts/…
const isShort = v => v.url.includes("/shorts/");
const pool = kind => kind === "videos" ? DATA.videos.filter(v => !isShort(v))
  : kind === "shorts" ? DATA.videos.filter(isShort) : DATA.articles;

function badge(id){
  if (id === "videos" || id === "shorts") return pool(id).filter(v => !isSeen(v)).length;
  if (id === "news") return DATA.articles.filter(a => !isSeen(a)).length;
  return 0;
}
function hint(id){
  if (id === "fairs"){
    const next = DATA.fairs.filter(f => f.start && daysTo(f.end || f.start) >= 0).sort((a,b) => a.start.localeCompare(b.start))[0];
    return next ? `через ${Math.max(0, daysTo(next.start))} дн.` : "";
  }
  return "";
}
function renderNav(){
  document.getElementById("nav").innerHTML = SECTIONS.map(s => {
    const n = badge(s.id), h = hint(s.id);
    return `<a href="#${s.id}" class="${s.id === current ? "on" : ""}">
      <span class="ic">${s.icon}</span><span class="lbl">${s.label}</span>
      ${n ? `<span class="badge">${n}</span>` : h ? `<span class="hint">${h}</span>` : ""}</a>`;
  }).join("");
}
window.addEventListener("hashchange", () => {
  current = location.hash.slice(1) || "videos";
  if (!SECTIONS.some(s => s.id === current)) current = "videos";
  store.set("artradar-section", current); render(); scrollTo(0, 0);
});

// ---------- общая лента (видео и новости) ----------
function feed(kind){
  const items = pool(kind), isVideo = kind !== "news";
  const topic = filters[kind];
  // Каналы галерей (Artseeker) закреплены первой строкой, поэтому отдельной темы для них нет.
  const topics = [...new Set(items.map(i => i.category).filter(c => c && c !== "galleries"))];
  const match = (i, t) => !t || (t.startsWith("ch:") ? i.channel_id === t.slice(3) : i.category === t);
  const shown = items.filter(i => match(i, topic));
  const fresh = shown.filter(i => !isSeen(i)), old = shown.filter(isSeen);
  const total = items.filter(i => !isSeen(i)).length;
  const sources = new Set(items.map(i => isVideo ? i.channel : i.source)).size;
  const v = document.getElementById("view");
  v.innerHTML = `
    <h1>${{videos:"Видео", shorts:"Шортсы", news:"Новости"}[kind]}</h1>
    <p class="sub">${greet()}! ${total ? {videos:"Новых видео: ", shorts:"Новых шортсов: ", news:"Новых статей: "}[kind] + total : "Всё новое просмотрено"}.
      ${isVideo ? "Каналов" : "Слежу за изданиями"}: ${sources}.</p>
    ${isVideo ? pinned(topic, items) : ""}
    <div class="chips">${["", ...topics].map(t => {
      const n = items.filter(i => !isSeen(i) && match(i, t)).length;
      return `<button class="chip ${t === topic ? "on" : ""}" data-topic="${t}">${t ? TOPIC_NAMES[t] || t : "Все"}${n ? ` <small>${n}</small>` : ""}</button>`;
    }).join("")}</div>
    <div class="bar"><span class="count">Новое для вас <span class="pill">${fresh.length}</span></span>
      ${fresh.length > 1 ? '<button id="allSeen">✓ Всё просмотрено</button>' : ""}</div>
    <div class="list ${kind}" id="fresh"></div>
    ${old.length ? `<details><summary>Уже просмотренные (${old.length}) ▾</summary><div class="list ${kind}" id="old"></div></details>` : ""}
    <p class="note">${isVideo ? "Видео и картинки — с YouTube. Нажмите «Смотреть», и видео откроется на YouTube."
      : "Статьи открываются на сайтах изданий. Многие издания пишут по-английски."}</p>`;
  v.querySelectorAll("[data-topic]").forEach(b => b.onclick = e => {
    if (e.target.closest("a")) return;  // ссылка «YouTube ↗» внутри закреплённой строки
    filters[kind] = filters[kind] === b.dataset.topic && b.dataset.topic ? "" : b.dataset.topic; render();
  });
  const mk = kind === "videos" ? videoCard : kind === "shorts" ? shortCard : articleCard;
  const fl = document.getElementById("fresh");
  // Показываем порциями, ниже — кнопка «Показать ещё».
  const more = (list, items, key, fresh) => {
    const n = limits[key] || PAGE;
    list.replaceChildren(...items.slice(0, n).map(i => mk(i, fresh)));
    if (items.length > n) {
      const b = document.createElement("button");
      b.className = "btn more";
      b.textContent = `Показать ещё (осталось ${items.length - n})`;
      b.onclick = () => { limits[key] = n + PAGE; render(); };
      list.append(b);
    }
  };
  if (fresh.length) more(fl, fresh, kind + ":fresh", true);
  else fl.innerHTML = '<div class="empty"><b>Всё просмотрено ✨</b>Как только появится новое, оно будет здесь.</div>';
  const ol = document.getElementById("old");
  if (ol) more(ol, old, kind + ":old", false);
  const all = document.getElementById("allSeen");
  if (all) all.onclick = () => { markSeen(fresh.map(i => i.id)); render(); };
}
// Закреплённая первая строка в «Видео»: каналы галереи Artseeker, как закреплённый чат в Telegram.
function pinned(topic, items){
  const chans = DATA.galleries.flatMap(g => g.youtube);
  return `<div class="pinned">${chans.map(c => {
    const t = "ch:" + c.channel_id;
    const n = items.filter(v => v.channel_id === c.channel_id && !isSeen(v)).length;
    return `<div class="pin ${topic === t ? "on" : ""}" data-topic="${t}" role="button" tabindex="0">
      <span class="pinic">📌</span>
      <span class="pinname"><b>${esc(c.name)}</b><span>Artseeker Gallery${c.lang === "ru" ? " · по-русски" : ""}</span></span>
      ${n ? `<span class="badge">${n}</span>` : ""}
      <a href="https://www.youtube.com/${esc(c.handle)}" target="_blank" rel="noopener" title="Открыть канал на YouTube">YouTube ↗</a>
    </div>`;
  }).join("")}</div>`;
}
function hookSeen(el, item){
  const mark = () => { markSeen([item.id]); render(); };
  el.querySelectorAll("a").forEach(a => a.addEventListener("click", () => setTimeout(mark, 300)));
  const b = el.querySelector(".mark"); if (b) b.onclick = mark;
  return el;
}
function videoCard(v, fresh){
  const el = document.createElement("article");
  el.className = "card vcard" + (fresh ? " new" : "");
  el.innerHTML = `
    <a class="thumb" href="${esc(v.url)}" target="_blank" rel="noopener">
      <img src="https://i.ytimg.com/vi/${esc(v.id)}/hqdefault.jpg" alt="" loading="lazy">
      ${fresh ? '<span class="newtag">Новое</span>' : ""}</a>
    <div>
      <div class="meta">${esc(v.channel)} · ${when(v.published)}</div>
      <p class="title">${esc(v.title)}</p>
      <div class="actions"><a class="btn main" href="${esc(v.url)}" target="_blank" rel="noopener">▶ Смотреть</a>
        ${fresh ? '<button class="mark">✓ Просмотрено</button>' : ""}</div>
    </div>`;
  return hookSeen(el, v);
}
function shortCard(v, fresh){
  const el = document.createElement("article");
  el.className = "card scard" + (fresh ? " new" : "");
  el.innerHTML = `
    <a class="thumb" href="${esc(v.url)}" target="_blank" rel="noopener">
      <img src="https://i.ytimg.com/vi/${esc(v.id)}/hqdefault.jpg" alt="" loading="lazy">
      ${fresh ? '<span class="newtag">Новое</span>' : ""}</a>
    <div class="meta">${esc(v.channel)} · ${when(v.published)}</div>
    <p class="title">${esc(v.title)}</p>
    ${fresh ? '<button class="mark">Просмотрено</button>' : ""}`;
  return hookSeen(el, v);
}
function articleCard(a, fresh){
  const el = document.createElement("article");
  el.className = "card" + (fresh ? " new" : "");
  el.innerHTML = `
    <div class="meta">${fresh ? '<span class="newtag">Новое</span>' : ""}${esc(a.source)} · ${when(a.published)}</div>
    <p class="title"><a href="${esc(a.id)}" target="_blank" rel="noopener">${esc(a.title)}</a></p>
    ${a.summary ? `<p class="text">${esc(a.summary)}</p>` : ""}
    <div class="actions"><a class="btn main" href="${esc(a.id)}" target="_blank" rel="noopener">Читать статью</a>
      ${fresh ? '<button class="mark">✓ Прочитано</button>' : ""}</div>`;
  return hookSeen(el, a);
}

// ---------- ярмарки ----------
function fairs(){
  const months = ["янв","фев","мар","апр","мая","июн","июл","авг","сен","окт","ноя","дек"];
  const dated = DATA.fairs.filter(f => f.start).sort((a,b) => a.start.localeCompare(b.start));
  const upcoming = dated.filter(f => daysTo(f.end || f.start) >= 0), past = dated.filter(f => daysTo(f.end || f.start) < 0);
  const tba = DATA.fairs.filter(f => !f.start);
  const card = (f, cls = "") => {
    const s = new Date(f.start + "T00:00:00"), e = new Date((f.end || f.start) + "T00:00:00");
    const d = daysTo(f.start), live = d <= 0 && daysTo(f.end || f.start) >= 0;
    return `<div class="card fair ${cls}">
      <div class="date"><b>${s.getDate()}–${e.getDate()}</b>${months[e.getMonth()]} ${e.getFullYear()}</div>
      <div><h3>${esc(f.name)}</h3><div class="meta">${esc(f.city)} · ${esc(f.venue)}</div>
        <a href="${esc(f.url)}" target="_blank" rel="noopener">Сайт ярмарки ↗</a></div>
      <div class="soon">${cls ? "прошла" : live ? "идёт сейчас" : `через ${d} дн.`}</div></div>`;
  };
  document.getElementById("view").innerHTML = `
    <h1>Ярмарки</h1><p class="sub">Главные ярмарки современного искусства в США.</p>
    <h2>Скоро</h2><div class="list">${upcoming.map(f => card(f)).join("") || '<div class="empty">Ближайших дат пока нет.</div>'}</div>
    <h2>Даты пока не объявлены</h2><div class="list">${tba.map(f => `<div class="card"><h3>${esc(f.name)}</h3>
      <div class="meta">${esc(f.city)} · ${esc(f.venue)}${f.note ? " · " + esc(f.note) : ""}</div>
      <a href="${esc(f.url)}" target="_blank" rel="noopener">Сайт ярмарки ↗</a></div>`).join("")}</div>
    ${past.length ? `<h2>Прошедшие</h2><div class="list">${past.map(f => card(f, "past")).join("")}</div>` : ""}`;
}

// ---------- форматы: мой размер ----------
const STD_IN = [[8,10],[9,12],[11,14],[12,12],[12,16],[16,20],[18,24],[20,20],[20,24],[24,24],[24,30],[24,36],[30,30],[30,40],[36,36],[36,48],[40,60],[48,48],[48,60],[60,72]];
const RATIOS = [[1,"1:1 (квадрат)"],[1.25,"5:4"],[4/3,"4:3"],[1.4,"7:5"],[1.5,"3:2"],[1.6,"8:5"],[2,"2:1"],[3,"3:1"]];
const sz = store.get("artradar-size", {h:60, w:80, unit:"cm"});
function formats(){
  document.getElementById("view").innerHTML = `
    <h1>Форматы</h1>
    <div class="chips"><button class="chip on">Мой размер</button><button class="chip" id="soldTab">Что покупают</button></div>
    <div id="fmt"></div>`;
  document.getElementById("soldTab").onclick = sold;
  mySize();
}
function mySize(){
  document.getElementById("fmt").innerHTML = `
    <p class="sub">Введите размер своей картины — посмотрим, как он выглядит и на какой американский стандарт похож.</p>
    <div class="sizebox">
      <div>
        <div class="units"><button class="chip" data-u="cm">Сантиметры</button><button class="chip" data-u="in">Дюймы</button></div>
        <div class="inputs">
          <div class="field"><label>Высота</label><input id="h" type="number" min="1" step="0.5" inputmode="decimal"></div>
          <div class="field"><label>Ширина</label><input id="w" type="number" min="1" step="0.5" inputmode="decimal"></div>
        </div>
        <div class="quick">${[[30,40],[40,50],[50,70],[60,80],[80,100],[100,100]].map(([a,b]) => `<button data-q="${a}x${b}">${a} × ${b} см</button>`).join("")}</div>
        <div class="answers" id="ans"></div>
      </div>
      <div class="stage easelbox big" id="cv"></div>
    </div>
    <p class="note">Это подсказка о размере и пропорциях. Какие форматы действительно продаются — во вкладке «Что покупают».</p>`;
  const h = document.getElementById("h"), w = document.getElementById("w");
  const show = () => {
    document.querySelectorAll("[data-u]").forEach(b => b.classList.toggle("on", b.dataset.u === sz.unit));
    h.value = sz.h; w.value = sz.w; update();
  };
  document.querySelectorAll("[data-u]").forEach(b => b.onclick = () => {
    if (b.dataset.u === sz.unit) return;
    const k = b.dataset.u === "in" ? 1/2.54 : 2.54;
    sz.h = +(sz.h * k).toFixed(1); sz.w = +(sz.w * k).toFixed(1); sz.unit = b.dataset.u; show();
  });
  document.querySelectorAll("[data-q]").forEach(b => b.onclick = () => {
    const [a, c] = b.dataset.q.split("x").map(Number); sz.unit = "cm"; sz.h = a; sz.w = c; show();
  });
  h.oninput = () => { sz.h = +h.value; update(); };
  w.oninput = () => { sz.w = +w.value; update(); };
  show();
}
function update(){
  store.set("artradar-size", sz);
  const ans = document.getElementById("ans"), cv = document.getElementById("cv");
  if (!(sz.h > 0 && sz.w > 0)) { ans.innerHTML = ""; return; }
  const hc = sz.unit === "cm" ? sz.h : sz.h * 2.54, wc = sz.unit === "cm" ? sz.w : sz.w * 2.54;
  const scale = 240 / Math.max(hc, wc);
  cv.innerHTML = easel(hc, wc, 420);
  const r = Math.max(hc, wc) / Math.min(hc, wc);
  const orient = Math.abs(r - 1) < 0.03 ? "квадрат" : hc > wc ? "вертикальная" : "горизонтальная";
  const long = Math.max(hc, wc);
  const group = long <= 61 ? "маленькая" : long <= 122 ? "средняя" : long <= 183 ? "большая" : "очень большая";
  const near = RATIOS.find(([x]) => Math.abs(r - x) / x <= 0.02);
  const f1 = n => (Math.round(n * 10) / 10).toString().replace(".", ",");
  const lo = Math.min(hc, wc), hi = Math.max(hc, wc);
  const std = STD_IN.map(([a, b]) => ({a, b, d: Math.abs(a * 2.54 - lo) + Math.abs(b * 2.54 - hi)})).sort((x, y) => x.d - y.d).slice(0, 3);
  const stdText = s => {
    const name = hc >= wc ? `${s.b} × ${s.a}` : `${s.a} × ${s.b}`;
    const cm = hc >= wc ? `${f1(s.b * 2.54)} × ${f1(s.a * 2.54)}` : `${f1(s.a * 2.54)} × ${f1(s.b * 2.54)}`;
    const diff = s.d < 2 ? "почти точно ваш размер" : `отличие около ${Math.round(s.d)} см по сторонам`;
    return `${name} дюймов (${cm} см) — ${diff}`;
  };
  ans.innerHTML = `
    <div><b>${f1(hc)} × ${f1(wc)} см = ${f1(hc / 2.54)} × ${f1(wc / 2.54)} дюймов</b>высота × ширина</div>
    <div><b>${orient[0].toUpperCase() + orient.slice(1)}, ${group}</b>${near ? "пропорция " + near[1] : "своя пропорция, примерно " + f1(r) + " : 1"}</div>
    <div><b>Ближайший американский стандарт</b>${stdText(std[0])}<br><span class="meta">также: ${std.slice(1).map(stdText).join("; ")}</span></div>`;
}
// ---------- рисунок «холст на мольберте» (вариант 4 от Codex, design/canvas_variants.html) ----------
// Лицевая сторона холста не искажается: высота / ширина = heightIn / widthIn.
let easelSeq = 0;
function easel(heightIn, widthIn, px = 200){
  const k = Math.min(83 / widthIn, 67 / heightIn), w = widthIn * k, h = heightIn * k, x = (180 - w) / 2, y = 80 - h;
  const id = "easel-" + (++easelSeq);
  const face = `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="url(#${id}-linen)"/>
    <rect x="${x}" y="${y}" width="${w}" height="${h}" fill="url(#${id}-weave)" opacity=".72"/>
    <path d="M${x} ${y + h}V${y}H${x + w}" fill="none" stroke="#fffdf3" stroke-width=".65" opacity=".8"/>`;
  const d = 4, depth = `<path d="M${x + w} ${y}l${d} ${-d * .6}v${h}l${-d} ${d * .6}Z" fill="url(#${id}-edge)"/>
    <path d="M${x} ${y}l${d} ${-d * .6}h${w}l${-d} ${d * .6}Z" fill="#f3e8d2"/>
    <path d="M${x + w} ${y}v${h}" stroke="#a99678" stroke-width=".55" fill="none"/>`;
  return `<svg class="easel" xmlns="http://www.w3.org/2000/svg" width="${px}" height="${px * 2 / 3}" viewBox="0 0 180 120" role="img"
      aria-label="Холст ${heightIn} × ${widthIn} дюймов на мольберте">
    <defs>
      <linearGradient id="${id}-linen" x2=".8" y2="1"><stop stop-color="#f5eddd"/><stop offset=".55" stop-color="#e9ddc5"/><stop offset="1" stop-color="#d8c9ac"/></linearGradient>
      <linearGradient id="${id}-edge"><stop stop-color="#b8a688"/><stop offset="1" stop-color="#d4c3a5"/></linearGradient>
      <pattern id="${id}-weave" width="2.3" height="2.3" patternUnits="userSpaceOnUse"><path d="M0 .45H2.3M.5 0V2.3" stroke="#7d694b" stroke-width=".32" opacity=".28"/><path d="M0 1.15H2.3M1.2 0V2.3" stroke="#fffaf0" stroke-width=".38" opacity=".55"/></pattern>
      <filter id="${id}-shadow" x="-70%" y="-70%" width="260%" height="260%" color-interpolation-filters="sRGB"><feDropShadow dx="2" dy="4" stdDeviation="3" flood-color="#100c08" flood-opacity=".24"/></filter>
      <filter id="${id}-blur" x="-50%" y="-200%" width="200%" height="500%"><feGaussianBlur stdDeviation="2.5"/></filter>
    </defs>
    <ellipse cx="92" cy="111" rx="38" ry="3" fill="#171009" opacity=".18" filter="url(#${id}-blur)"/>
    <g stroke-linejoin="round"><path d="M92 27L116 110" stroke="#8e6339" stroke-width="4"/><path d="M86 20L64 111M94 20L113 111" stroke="#bd8d53" stroke-width="5"/><path d="M70 96H108" stroke="#a67640" stroke-width="4"/><path d="M90 9V99" stroke="#d1a369" stroke-width="6"/><path d="M89 10V98" stroke="#f0ca8c" stroke-width=".8"/>
    <g filter="url(#${id}-shadow)">${depth}${face}</g>
    <path d="M${x - 5} 82H${x + w + 7}" stroke="#8f6236" stroke-width="5"/><path d="M${x - 5} 80H${x + w + 7}" stroke="#e0b67a" stroke-width="2"/>
    <rect x="84" y="${y - 3}" width="12" height="5" rx=".8" fill="#ba894f"/><circle cx="90" cy="${y - .5}" r="1" fill="#6f5840"/><circle cx="90" cy="91" r="1.5" fill="#786044"/></g>
  </svg>`;
}

// ---------- форматы: что покупают (только подтверждённые продажи) ----------
const MIN_SHARE = 30, MIN_PRICE = 10;  // меньше примеров — проценты и цены не показываем
const BUCKETS = {S:["маленькие","до 61 см"], M:["средние","61–122 см"], L:["большие","122–183 см"], XL:["очень большие","больше 183 см"]};
const ORIENT = {vertical:["Вертикальные",3,4], horizontal:["Горизонтальные",4,3], square:["Квадрат",1,1]};
const api = (path, body) => fetch(path, {method:"POST", headers:{"Content-Type":"application/json", "X-Radar":"1"},
  body: JSON.stringify(body)}).then(r => r.json());
const fmt1 = n => (Math.round(n * 10) / 10).toString().replace(".", ",");
const money = (n, cur) => (cur === "USD" ? "$" : "") + Math.round(n).toLocaleString("ru-RU") + (cur && cur !== "USD" ? " " + cur : "");
const median = a => { const s = [...a].sort((x, y) => x - y), m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
function plural(n, one, few, many){ const m = n % 10, h = n % 100; return m === 1 && h !== 11 ? one : m >= 2 && m <= 4 && (h < 12 || h > 14) ? few : many; }
function share(n, total){ return total >= MIN_SHARE ? `примерно ${Math.round(n / total * 100)} из 100 проданных` : `${n} ${plural(n, "работа", "работы", "работ")}`; }
function canvasBox(h, w, max = 120){ const k = max / Math.max(h, w); return `<div class="canvas" style="height:${h * k}px;width:${w * k}px"></div>`; }
function priceLine(recs){
  const usd = recs.filter(r => r.p && r.cur === "USD" && r.prem);
  return usd.length >= MIN_PRICE ? `обычная цена: ${money(median(usd.map(r => r.p)), "USD")}`
    : `цена: мало примеров (${usd.length} из ${MIN_PRICE})`;
}

const CATS = {painting:["🖌", "Живопись"], graphics:["✏️", "Графика"], mixed:["🧩", "Смешанная техника"]};
const PERIODS = [["", "Все годы", () => true], ["2010", "2010–2015", y => y >= 2010], ["2000", "2000–2009", y => y >= 2000 && y < 2010],
                 ["1999", "до 2000", y => y && y < 2000]];
let soldCat = "", soldPeriod = "";
const nWorks = n => `${n.toLocaleString("ru-RU")} ${plural(n, "работа", "работы", "работ")}`;

function sold(){
  document.querySelectorAll(".chips .chip").forEach((b, i) => b.classList.toggle("on", i === 1));
  document.querySelectorAll(".chips .chip")[0].onclick = formats;
  const period = PERIODS.find(p => p[0] === soldPeriod)[2];
  const inPeriod = (DATA.sales || []).filter(r => period(r.y));
  const recs = inPeriod.filter(r => !soldCat || r.c === soldCat);
  const N = recs.length, TOTAL = inPeriod.length;
  const years = (DATA.sales || []).map(r => r.y).filter(Boolean);
  const own = DATA.sales_own || [];
  const box = document.getElementById("fmt");
  const dataBlock = `<details class="datablock"><summary>Откуда данные и как пополнить ▾</summary>
      ${(DATA.datasets || []).map(d => `<p class="text"><b>${esc(d.name)}</b> — ${esc(d.about)}, аукционы в долларах США (в основном Нью-Йорк).
        <a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)} ↗</a></p>`).join("")}
      ${DATA.static ? "" : `<p class="text">Ваши добавленные продажи: ${own.length}.</p>
      <div class="actions" style="margin-bottom:14px">
        <button class="btn main" id="addSale">＋ Добавить продажу</button>
        <button class="btn" id="importSales">Загрузить таблицу</button></div>
      <div class="list">${own.map(r => `<div class="card salerow">
        <div><b>${esc(r.artist_name)}</b>${r.artwork_title ? " — «" + esc(r.artwork_title) + "»" : ""}
          <div class="meta">${Math.round(r.height_cm)} × ${Math.round(r.width_cm)} см · ${CATS[r.category][1].toLowerCase()} (${esc(r.medium_raw || r.technique_ru)}) ·
            ${r.sale_price ? money(r.sale_price, r.currency) : "цена не указана"} · ${esc(r.source_name || "")} ${esc(r.sale_date || "")}</div></div>
        <div class="actions"><a class="btn" href="${esc(r.source_url)}" target="_blank" rel="noopener">Источник ↗</a>
          <button class="del" data-id="${r.id}">Удалить</button></div></div>`).join("")}</div>`}</details>`;

  if (!(DATA.sales || []).length) {
    box.innerHTML = `<div class="empty"><b>Пока нет данных о проданных работах</b>
        Здесь будет статистика: что продаётся — живопись, графика или смешанная техника — и в каком формате.</div>${dataBlock}`;
  } else {
    const count = cat => inPeriod.filter(r => r.c === cat).length;
    // Размеры группируем по целым дюймам — так их называют в США (24 × 30 и т. п.).
    const bySize = {};
    recs.forEach(r => {
      const key = Math.round(r.h / 2.54) + "x" + Math.round(r.w / 2.54);
      (bySize[key] = bySize[key] || []).push(r);
    });
    const top = Object.entries(bySize).sort((a, b) => b[1].length - a[1].length).slice(0, 12);
    box.innerHTML = `
      <p class="sub">Подтверждённые аукционные продажи в США: <b>${nWorks(TOTAL)}</b>
        за ${soldPeriod ? PERIODS.find(p => p[0] === soldPeriod)[1].replace("до 2000", "годы до 2000") : `${Math.min(...years)}–${Math.max(...years)}`}${soldPeriod === "1999" ? "" : " годы"}.</p>
      <div class="chips">${PERIODS.map(([k, name]) => `<button class="chip ${k === soldPeriod ? "on" : ""}" data-period="${k}">${name}</button>`).join("")}</div>
      <h2>Что продаётся</h2>
      <div class="cats">${[["", ["🖼", "Всё"]], ...Object.entries(CATS).filter(([k]) => count(k))].map(([k, [ic, name]]) => {
        const n = k ? count(k) : TOTAL;
        return `<button class="cat ${k === soldCat ? "on" : ""}" data-cat="${k}" ${n ? "" : "disabled"}>
          <span class="catic">${ic}</span><b>${name}</b>
          <span>${!n ? "нет данных: в открытых источниках техника не указана" : k ? share(n, TOTAL) + "<br>" + nWorks(n) : nWorks(n)}</span></button>`;
      }).join("")}</div>
      <h2>Какой формат${soldCat ? " — " + CATS[soldCat][1].toLowerCase() : ""}</h2>
      <p class="meta">12 самых частых размеров (высота × ширина). Размеры очень разнообразны, поэтому доля каждого небольшая.</p>
      ${N ? `<div class="sizes">${top.map(([key, rs]) => {
        const [hi, wi] = key.split("x").map(Number);
        return `<div class="card size"><div class="stage small easelbox">${easel(hi, wi)}</div>
          <b class="big">${Math.round(hi * 2.54)} × ${Math.round(wi * 2.54)} см</b><span class="meta">${hi} × ${wi} дюймов</span>
          <span>${share(rs.length, N)}</span><span class="meta">${nWorks(rs.length)} · ${priceLine(rs)}</span></div>`;
      }).join("")}</div>
      <h2>Форма и величина</h2>
      <div class="sizes">${Object.entries(ORIENT).map(([k, [name, w, h]]) => `<div class="card size">
        <div class="stage small easelbox">${easel(h, w)}</div><b>${name}</b><span>${share(recs.filter(r => r.o === k).length, N)}</span></div>`).join("")}
        ${Object.entries(BUCKETS).map(([k, [name, range]], i) => `<div class="card size">
        <div class="stage small">${canvasBox(4, 5, 30 + i * 22)}</div><b>${name[0].toUpperCase() + name.slice(1)}</b><span class="meta">${range}</span>
        <span>${share(recs.filter(r => r.b === k).length, N)}</span></div>`).join("")}</div>`
      : '<div class="empty">В этой группе пока нет продаж.</div>'}
      <p class="note">Только подтверждённые продажи с аукционов. Это в основном работы известных художников и перепродажа;
        галерейные продажи могут отличаться. «Обычная цена» — середина цен с комиссией покупателя.</p>
      ${dataBlock}`;
    box.querySelectorAll("[data-cat]").forEach(b => b.onclick = () => { soldCat = b.dataset.cat; sold(); });
    box.querySelectorAll("[data-period]").forEach(b => b.onclick = () => { soldPeriod = b.dataset.period; sold(); });
  }
  box.querySelectorAll(".del").forEach(b => b.onclick = async () => {
    if (!confirm("Удалить эту продажу из базы?")) return;
    applySales(await api("/api/sales/delete", {id: b.dataset.id})); sold();
  });
  if (!DATA.static) {
    document.getElementById("addSale").onclick = addSaleDialog;
    document.getElementById("importSales").onclick = importDialog;
  }
}
function applySales(r){ if (r.sales) { DATA.sales = r.sales; DATA.sales_own = r.sales_own; } }

function dialog(html){
  const d = document.createElement("dialog");
  d.innerHTML = `<form method="dialog" class="dlg">${html}</form>`;
  document.body.append(d); d.showModal();
  d.addEventListener("close", () => d.remove());
  return d;
}
function addSaleDialog(){
  const d = dialog(`
    <h2 style="margin-top:0">Добавить продажу</h2>
    <label class="lbl">1. Вставьте текст лота со страницы аукциона</label>
    <textarea id="lotText" rows="7" placeholder="Например:&#10;Имя художника&#10;Название работы&#10;oil on canvas&#10;40 x 30 in. (101.6 x 76.2 cm.)&#10;Price realised USD 12,600"></textarea>
    <button type="button" class="btn" id="parse">Разобрать текст</button>
    <div id="warn"></div>
    <label class="lbl">2. Проверьте и поправьте</label>
    <div class="grid2">
      <label>Художник<input name="artist_name"></label><label>Название<input name="artwork_title"></label>
      <label>Высота, см<input name="height_cm" inputmode="decimal"></label><label>Ширина, см<input name="width_cm" inputmode="decimal"></label>
      <label>Техника (как в источнике)<input name="medium_raw" placeholder="oil on canvas"></label><label>Дата продажи<input name="sale_date" type="date"></label>
      <label>Цена продажи<input name="sale_price" inputmode="decimal"></label>
      <label>Валюта<select name="currency"><option>USD</option><option>EUR</option><option>GBP</option><option>HKD</option></select></label>
      <label>Аукцион / галерея<input name="source_name"></label><label>Город<input name="city" placeholder="New York"></label>
    </div>
    <label>Ссылка на страницу, где видно, что работа продана<input name="source_url" placeholder="https://..."></label>
    <label>Откуда продажа<select name="confirmation_level">
      <option value="A1">Результат аукциона (Sold / Price realised)</option>
      <option value="A2">Документ о продаже (галерея, мои продажи)</option></select></label>
    <label class="check"><input type="checkbox" name="sold_confirmed"> Продажа подтверждена: в источнике прямо сказано, что работа продана</label>
    <p class="meta">Не добавляйте работы, которые просто пропали с сайта или «недоступны» — это ещё не продажа.</p>
    <div id="err" class="err"></div>
    <div class="actions"><button type="button" class="btn main" id="save">Сохранить</button><button class="btn" value="cancel">Отмена</button></div>`);
  const form = d.querySelector("form");
  const set = (k, v) => { if (form.elements[k] && v != null) form.elements[k].value = v; };
  d.querySelector("#parse").onclick = async () => {
    const r = await api("/api/sales/parse", {text: d.querySelector("#lotText").value});
    ["artist_name","artwork_title","height_cm","width_cm","medium_raw","sale_date","sale_price","currency","source_name","source_url"].forEach(k => set(k, r[k]));
    d.querySelector("#warn").innerHTML = (r.warnings || []).map(w => `<p class="warn">⚠ ${esc(w)}</p>`).join("") ||
      '<p class="ok">✓ Всё нашлось. Проверьте поля и добавьте ссылку.</p>';
    d.unsold = !!r.unsold; d.premium = r.price_includes_premium;
  };
  d.querySelector("#save").onclick = async () => {
    const data = Object.fromEntries(new FormData(form));
    data.sold_confirmed = form.elements.sold_confirmed.checked;
    data.unsold = d.unsold; data.price_includes_premium = d.premium ?? (data.confirmation_level === "A1");
    for (const k of ["height_cm", "width_cm", "sale_price"]) if (data[k]) data[k] = data[k].replace(",", ".").replace(/\s/g, "");
    const r = await api("/api/sales/save", {...data, entry_method: d.querySelector("#lotText").value ? "paste" : "manual"});
    if (r.error) { d.querySelector("#err").textContent = r.error; return; }
    applySales(r); d.close(); sold();
  };
}
function importDialog(){
  const d = dialog(`
    <h2 style="margin-top:0">Загрузить таблицу продаж</h2>
    <p class="text">Подойдёт таблица из Excel или Google Таблиц, сохранённая как CSV. Названия столбцов — первой строкой:<br>
      <code>artist_name, artwork_title, height_cm, width_cm</code> (или <code>height_in, width_in</code>),
      <code>medium_raw, sale_price, currency, sale_date, source_name, source_url</code>.<br>
      В каждой строке обязательна ссылка-подтверждение <code>source_url</code>.</p>
    <input type="file" id="csvFile" accept=".csv,text/csv">
    <div id="res"></div>
    <div class="actions"><button type="button" class="btn main" id="go">Загрузить</button><button class="btn" value="cancel">Закрыть</button></div>`);
  d.querySelector("#go").onclick = async () => {
    const f = d.querySelector("#csvFile").files[0]; if (!f) return;
    const r = await api("/api/sales/import", {text: await f.text()});
    applySales(r); sold();
    d.querySelector("#res").innerHTML = `<p class="ok">Добавлено: ${r.added}</p>` + (r.errors || []).map(e => `<p class="warn">⚠ ${esc(e)}</p>`).join("");
  };
}

// ---------- запуск ----------
const VIEWS = {videos: () => feed("videos"), shorts: () => feed("shorts"), news: () => feed("news"), fairs, formats};
function render(){ renderNav(); VIEWS[current](); }
render();

// ---------- свежие данные с сервера ----------
function setStatus(cls, text){
  for (const [id, base] of [["status", "status"], ["status2", "status-m"]]) {
    const el = document.getElementById(id); el.className = base + (cls ? " " + cls : ""); el.textContent = text;
  }
}
if (DATA.static) {
  const b = new Date(DATA.built_at);
  setStatus("", "Обновлено " + b.toLocaleDateString("ru-RU", {day:"numeric", month:"long"})
    + " в " + b.toLocaleTimeString("ru-RU", {hour:"2-digit", minute:"2-digit"}));
} else if (location.protocol.startsWith("http")) {
  setStatus("busy", "Обновляю…");
  fetch("/api/refresh").then(r => r.json()).then(fresh => {
    Object.assign(DATA, fresh);
    // Раздел «Форматы» не перерисовываем, чтобы не сбить ввод размера.
    if (current === "formats") renderNav(); else render();
    setStatus("", "Обновлено в " + new Date().toLocaleTimeString("ru-RU", {hour:"2-digit", minute:"2-digit"}));
  }).catch(() => setStatus("", "Нет связи — показано сохранённое"));
  setInterval(() => fetch("/api/ping").catch(() => {}), 60000);
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
