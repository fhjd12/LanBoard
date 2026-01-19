import os, re, json, uuid, time, socket, threading, io, webbrowser, sys
from typing import Dict, Any, List, Set
from pathlib import Path
from urllib.parse import urlparse

import qrcode
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn

import pystray
from PIL import Image, ImageDraw

# ================= App 信息 =================
APP_NAME = "LanBoard"

def get_base_dir() -> Path:
    """返回程序运行基准目录（兼容 .py / PyInstaller onedir .exe）

    - .exe: 以 exe 所在目录为基准（旁边生成 uploads/ data/ config.json）
    - .py: 以仓库根目录为基准（假设脚本位于 <root>/src/ 下）
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # <root>/src/lan_board.py -> parents[1] == <root>
    return Path(__file__).resolve().parents[1]

BASE_DIR = get_base_dir()
UPLOAD_DIR = BASE_DIR / "uploads"
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.json"
VERSION_PATH = BASE_DIR / "version.txt"

def resource_path(rel: str) -> Path:
    """兼容 .py / .exe 的资源路径解析

    PyInstaller 会把 --add-data 的资源解压到 sys._MEIPASS。
    本函数优先从 _MEIPASS 取，其次从 BASE_DIR 取。
    """
    rel = rel.lstrip("/\\")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / rel
        if p.exists():
            return p
    return BASE_DIR / rel

def read_version(default: str = "1.1.1") -> str:
    try:
        v = VERSION_PATH.read_text(encoding="utf-8").strip()
        return v or default
    except Exception:
        return default

APP_VERSION = read_version()

# 目录自检：exe 启动时自动在同级生成 uploads / data / config.json
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "app_name": APP_NAME,
    "version": APP_VERSION,
    "host": "0.0.0.0",
    "port": 8787,
    "password": "1234",

    # 附件清理
    "retention_hours": 24,
    "clean_interval_hours": 24,

    # 业务参数
    "max_file_mb": 30,        # 你现在还是 30MB；想“无限制”可改成 0 或删掉限制逻辑
    "history_limit": 800,
}


def load_or_create_config() -> dict:
    # 确保目录存在
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)

    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        # 配置损坏：备份并重建
        bak = BASE_DIR / f"config.bad.{int(time.time())}.json"
        try:
            CONFIG_PATH.rename(bak)
        except Exception:
            pass
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)

    # 补齐缺省键（未来升级非常有用）
    changed = False
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if changed:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    return cfg


CFG = load_or_create_config()

# 允许环境变量覆盖（可选：你原来就用 env，我保留它但不强制）
HOST = str(os.environ.get("LAN_HOST", CFG.get("host", "0.0.0.0")))
PORT = int(os.environ.get("LAN_PORT", str(CFG.get("port", 8787))))
PASSWORD = str(os.environ.get("LAN_PASSWORD", CFG.get("password", "1234"))).strip() or "1234"

MAX_FILE_MB = int(CFG.get("max_file_mb", 30))
HISTORY_LIMIT = int(CFG.get("history_limit", 800))

RETENTION_SECONDS = int(float(CFG.get("retention_hours", 24)) * 3600)
CLEAN_INTERVAL_SECONDS = int(float(CFG.get("clean_interval_hours", 24)) * 3600)


def self_check_or_die():
    # 1) data 目录可写
    try:
        test_file = DATA_DIR / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
    except Exception as e:
        raise RuntimeError(f"[self-check] data 目录不可写：{DATA_DIR} | {e}")

    # 2) 端口可用（未被占用）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("0.0.0.0", PORT))
        s.close()
    except OSError as e:
        raise RuntimeError(f"[self-check] 端口 {PORT} 被占用或无权限：{e}")


# ================= FastAPI 初始化 =================
app = FastAPI()
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


# ================= 全局状态（无房间） =================
state: Dict[str, Any] = {
    "clients": set(),   # type: Set[WebSocket]
    "history": [],      # type: List[Dict[str, Any]]
}
HISTORY_FILE = DATA_DIR / "history.jsonl"


# ================= 工具函数 =================
def tray_tip(s: str, limit: int = 120) -> str:
    s = (s or "").replace("\r", " ").replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[:limit - 1] + "…"

def now_ms() -> int:
    return int(time.time() * 1000)

def safe_ext(filename: str) -> str:
    _, ext = os.path.splitext(filename or "")
    ext = ext.lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,10}", ext) else ""

def is_image(filename: str) -> bool:
    return os.path.splitext(filename or "")[1].lower() in [".png",".jpg",".jpeg",".gif",".webp",".bmp"]

def normalize_upload_url(url: str) -> str:
    """
    统一把url变成形如 /uploads/2026-01-17/xxx.zip 的形式
    兼容:
    - /uploads/2026-01-17/xxx.zip
    - http://192.168.x.x:8787/uploads/...(带域名)
    - Windows 反斜杠
    """
    url = (url or "").strip()
    url = url.replace("\\", "/")
    if url.startswith("http://") or url.startswith("https://"):
        try:
            p = urlparse(url)
            url = p.path or url
        except Exception:
            pass
    return url

def url_to_local_path(url: str) -> str:
    """
    把 /uploads/2026-01-17/xxx.png 映射到本地文件路径，并防止越权路径
    """
    url = normalize_upload_url(url)
    if not url.startswith("/uploads/"):
        return ""
    rel = url[len("/uploads/"):]  # e.g. 2026-01-17/xxx.png
    rel = rel.lstrip("/\\")
    local = (UPLOAD_DIR / rel).resolve()
    base = UPLOAD_DIR.resolve()
    # 防止 ../ 越权
    try:
        local.relative_to(base)
    except Exception:
        return ""
    return str(local)

def delete_attachments_files(attachments: List[Dict[str, Any]]) -> int:
    deleted = 0
    for a in attachments or []:
        try:
            url = str(a.get("url", ""))
            p = url_to_local_path(url)
            if p and os.path.isfile(p):
                os.remove(p)
                deleted += 1
        except Exception:
            pass
    return deleted

def purge_uploads_all() -> int:
    deleted = 0
    for root, dirs, files in os.walk(str(UPLOAD_DIR), topdown=False):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                os.remove(fp)
                deleted += 1
            except Exception:
                pass
        for d in dirs:
            dp = os.path.join(root, d)
            try:
                os.rmdir(dp)
            except Exception:
                pass
    return deleted

def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def check_password(p: str):
    if p != PASSWORD:
        raise HTTPException(status_code=403, detail="Forbidden")

def load_history():
    if not HISTORY_FILE.exists():
        return
    items: List[Dict[str, Any]] = []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        return

    if len(items) > HISTORY_LIMIT:
        items = items[-HISTORY_LIMIT:]
    state["history"] = items

def rewrite_history_file():
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        for it in state["history"]:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

def append_history(item: Dict[str, Any]):
    h = state["history"]
    h.append(item)
    if len(h) > HISTORY_LIMIT:
        del h[0:len(h)-HISTORY_LIMIT]
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")

async def broadcast(payload: Dict[str, Any]):
    data = json.dumps(payload, ensure_ascii=False)
    dead = []
    for c in list(state["clients"]):
        try:
            await c.send_text(data)
        except Exception:
            dead.append(c)
    for d in dead:
        state["clients"].discard(d)


# ================= 附件清理 =================
def cleanup_uploads_once():
    now = time.time()
    deleted = 0

    for root, dirs, files in os.walk(str(UPLOAD_DIR), topdown=False):
        for fn in files:
            path = os.path.join(root, fn)
            try:
                if now - os.path.getmtime(path) > RETENTION_SECONDS:
                    os.remove(path)
                    deleted += 1
            except Exception:
                pass

        for d in dirs:
            dp = os.path.join(root, d)
            try:
                os.rmdir(dp)
            except Exception:
                pass

    if deleted:
        print(f"[cleanup] removed {deleted} expired files")


# ================= 页面（单页消息板） =================
PAGE_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>LanBoard</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;margin:12px;background:#fafafa;}
    .wrap{max-width:980px;margin:0 auto;}
    .top{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px;}
    .muted{font-size:12px;color:#666}
    .box{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:12px;}
    #log{height:58vh;overflow:auto;display:flex;flex-direction:column;gap:10px;padding:6px;}
    .msg{background:#f6f7f9;border:1px solid #ececec;border-radius:10px;padding:10px;position:relative;}
    .meta{font-size:12px;color:#666;margin-bottom:6px;display:flex;gap:10px;flex-wrap:wrap;}
    .text{white-space:pre-wrap;word-break:break-word;font-size:15px;}
    .att{margin-top:8px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
    .att img{max-width:240px;max-height:240px;border-radius:10px;border:1px solid #eee;}
    textarea{width:100%;height:88px;font-size:15px;line-height:1.4;padding:10px;border-radius:10px;border:1px solid #ddd;box-sizing:border-box;}
    button,input{font-size:15px;padding:10px 12px;border-radius:10px;border:1px solid #ddd;background:#fff;}
    button{cursor:pointer}
    .drop{margin-top:10px;border:2px dashed #cfcfcf;border-radius:12px;padding:10px;background:#fff;color:#666;font-size:13px;}
    .drop.dragover{border-color:#6aa9ff;color:#2b6bff;background:#f3f8ff;}
    .queue{margin-top:8px;display:flex;flex-direction:column;gap:6px;}
    .qitem{font-size:13px;color:#444;background:#fff;border:1px solid #eee;border-radius:10px;padding:8px;display:flex;justify-content:space-between;gap:10px;}
    .rm{cursor:pointer;color:#b00020}
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
    .danger{border-color:#ffb4b4;background:#fff5f5;}
    .smallbtn{padding:6px 10px;font-size:13px;border-radius:9px}
    .delbtn{position:absolute;top:8px;right:8px}
    .addr{max-width:520px;word-break:break-all}
  </style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="muted">
      状态：<span id="st">连接中…</span>
      ｜ <button class="smallbtn danger" id="clearBtn">清屏</button>
    </div>
    <div class="row">
      <span class="muted">扫码进入：</span>
      <img id="qr" alt="QR" style="width:96px;height:96px;border:1px solid #eee;border-radius:10px;background:#fff"/>
      <div>
        <div class="muted">地址：</div>
        <div class="muted addr" id="addr"></div>
      </div>
    </div>
  </div>

  <div class="box"><div id="log"></div></div>

  <div class="drop" id="drop">
    拖拽文件到这里（支持多文件）加入待发送队列
    <div class="queue" id="queue"></div>
  </div>

  <div style="margin-top:10px">
    <textarea id="text" placeholder="输入消息…（支持多行）"></textarea>
    <div class="row" style="margin-top:8px;justify-content:space-between">
      <div class="row">
        <input id="file" type="file" multiple />
        <button id="send">发送</button>
      </div>
      <div class="muted" id="hint"></div>
    </div>
  </div>
</div>

<script>
const PASS = "__PASS__";
const FULL_ADDR = "__FULL_ADDR__";

function setStatus(t){ st.textContent=t; }
function deviceName(){
  const ua = navigator.userAgent;
  if (ua.includes("iPhone")) return "iPhone";
  if (ua.includes("iPad")) return "iPad";
  if (ua.includes("Android")) return "Android";
  return "PC";
}
function fmtTime(ms){ return new Date(ms).toLocaleTimeString(); }

function renderMsg(m){
  const card = document.createElement("div");
  card.className = "msg";
  card.dataset.id = m.id;

  const del = document.createElement("button");
  del.className = "smallbtn danger delbtn";
  del.textContent = "删除";
  del.onclick = async () => {
    if(!confirm("确定删除这条消息？")) return;
    try{
      const r = await fetch(`/${encodeURIComponent(PASS)}/api/msg/${encodeURIComponent(m.id)}`, {method:"DELETE"});
      if(!r.ok) alert(await r.text());
    }catch(e){
      alert(String(e));
    }
  };
  card.appendChild(del);

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `${fmtTime(m.ts)}  来自：${m.sender||""}  #${m.id.slice(0,6)}`;
  card.appendChild(meta);

  if (m.text){
    const t = document.createElement("div");
    t.className = "text";
    t.textContent = m.text;
    card.appendChild(t);
  }

  if (m.attachments && m.attachments.length){
    const att = document.createElement("div");
    att.className = "att";
    for (const a of m.attachments){
      if (a.kind === "image"){
        const img = document.createElement("img");
        img.src = a.url;
        att.appendChild(img);
      }
      const link = document.createElement("a");
      link.href = a.url;
      link.textContent = `下载：${a.name}`;
      link.download = a.name;   // 强制下载
      att.appendChild(link);
    }
    card.appendChild(att);
  }

  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
}

function removeMsgDom(id){
  const el = log.querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (el) el.remove();
}

let pendingFiles = [];
function renderQueue(){
  queue.innerHTML = "";
  pendingFiles.forEach((f, idx) => {
    const row = document.createElement("div");
    row.className = "qitem";
    const left = document.createElement("span");
    left.textContent = `${f.name} (${Math.round(f.size/1024)}KB)`;
    const rm = document.createElement("span");
    rm.className = "rm";
    rm.textContent = "移除";
    rm.onclick = () => { pendingFiles.splice(idx,1); renderQueue(); };
    row.appendChild(left);
    row.appendChild(rm);
    queue.appendChild(row);
  });
}
function addFiles(files){
  for (const f of files){
    if (!pendingFiles.some(x => x.name===f.name && x.size===f.size && x.lastModified===f.lastModified)){
      pendingFiles.push(f);
    }
  }
  renderQueue();
}

file.onchange = () => {
  if (file.files && file.files.length){
    addFiles(Array.from(file.files));
    file.value = "";
  }
};
drop.addEventListener("dragover", (e)=>{ e.preventDefault(); drop.classList.add("dragover"); });
drop.addEventListener("dragleave", ()=> drop.classList.remove("dragover"));
drop.addEventListener("drop", (e)=>{
  e.preventDefault();
  drop.classList.remove("dragover");
  const files = Array.from(e.dataTransfer.files || []);
  if (files.length) addFiles(files);
});

async function uploadOne(f){
  const fd = new FormData();
  fd.append("pass", PASS);
  fd.append("up", f);
  const resp = await fetch(`/${encodeURIComponent(PASS)}/upload`, { method:"POST", body: fd });
  if(!resp.ok){ throw new Error(await resp.text() || "上传失败"); }
  return await resp.json();
}
async function uploadMany(files){
  const out = [];
  for(let i=0;i<files.length;i++){
    hint.textContent = `上传 ${i+1}/${files.length}：${files[i].name}`;
    out.push(await uploadOne(files[i]));
  }
  hint.textContent = "";
  return out;
}

// ====== WebSocket（iOS 优化最终版）======
let ws = null;
let retry = 0;
let gen = 0;
let reconnectTimer = null;

function connect(){
  gen += 1;
  const myGen = gen;

  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

  if (ws) {
    try { ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null; ws.close(); } catch(e){}
    ws = null;
  }

  setStatus("连接中…");

  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
  const url = `ws://${location.host}/ws?pass_=${encodeURIComponent(PASS)}`;

  const sock = new WebSocket(url);
  ws = sock;

  const quickRedialMs = isIOS ? 800 : 0;
  const redialTimer = quickRedialMs ? setTimeout(() => {
    if (myGen !== gen) return;
    if (ws && ws.readyState === 0) {
      setStatus("连接较慢，快速重试…");
      try { ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null; ws.close(); } catch(e){}
      ws = null;
      connect();
    }
  }, quickRedialMs) : null;

  const handshakeTimeoutMs = isIOS ? 1500 : 3500;
  const hsTimer = setTimeout(() => {
    if (myGen !== gen) return;
    if (ws && ws.readyState !== 1) {
      setStatus("连接超时，正在重试…");
      try { ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null; ws.close(); } catch(e){}
      ws = null;
      reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, 50);
    }
  }, handshakeTimeoutMs);

  sock.onopen = () => {
    if (myGen !== gen) return;
    clearTimeout(hsTimer); if (redialTimer) clearTimeout(redialTimer);
    retry = 0;
    setStatus("已连接");
  };

  sock.onerror = () => {
    if (myGen !== gen) return;
    clearTimeout(hsTimer); if (redialTimer) clearTimeout(redialTimer);
    setStatus("连接错误，重试中…");
  };

  sock.onclose = () => {
    if (myGen !== gen) return;
    clearTimeout(hsTimer); if (redialTimer) clearTimeout(redialTimer);
    setStatus("连接断开，重连中…");
    retry = Math.min(retry + 1, 6);
    const delay = Math.min(300 * (2 ** retry), 10000);
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, delay);
  };

  sock.onmessage = (ev) => {
    if (myGen !== gen) return;
    const msg = JSON.parse(ev.data);
    if(msg.type==="history"){
      log.innerHTML = "";
      msg.items.forEach(renderMsg);
    } else if(msg.type==="msg"){
      renderMsg(msg.item);
    } else if(msg.type==="delete"){
      removeMsgDom(msg.id);
    } else if(msg.type==="clear"){
      log.innerHTML = "";
    } else if(msg.type==="error"){
      alert(msg.message || "错误");
    }
  };
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) connect();
});

clearBtn.onclick = async () => {
  if(!confirm("确认清屏？（将删除所有消息记录）")) return;
  try{
    const r = await fetch(`/${encodeURIComponent(PASS)}/api/clear`, {method:"POST"});
    if(!r.ok) alert(await r.text());
  }catch(e){
    alert(String(e));
  }
};

send.onclick = async () => {
  if(!ws || ws.readyState !== 1) return alert("未连接到服务器");

  send.disabled = true;
  try{
    const msgText = (text.value || "").trim();
    const files = pendingFiles.slice();
    if(!msgText && files.length===0) return;

    const attachments = await uploadMany(files);
    pendingFiles = []; renderQueue();

    ws.send(JSON.stringify({
      type:"msg",
      pass: PASS,
      sender: deviceName(),
      text: msgText,
      attachments
    }));
    text.value = "";
  } catch(e){
    alert(String(e.message || e));
  } finally{
    send.disabled = false;
    hint.textContent = "";
  }
};

(function init(){
  addr.textContent = FULL_ADDR;
  qr.src = `/${encodeURIComponent(PASS)}/qr.png`;
  connect();
})();
</script>
</body>
</html>
"""


# ================= 路由：入口必须带密码 =================
@app.get("/", response_class=HTMLResponse)
def root():
    ip = get_lan_ip()
    return HTMLResponse(
        f"请在地址栏输入： http://{ip}:{PORT}/<密码>",
        status_code=200
    )

@app.get("/{p}", response_class=HTMLResponse)
def page(p: str):
    check_password(p)
    ip = get_lan_ip()
    full = f"http://{ip}:{PORT}/{PASSWORD}"
    html = PAGE_HTML.replace("__PASS__", PASSWORD).replace("__FULL_ADDR__", full)
    return HTMLResponse(html)

@app.get("/{p}/qr.png")
def qr_png(p: str):
    check_password(p)
    ip = get_lan_ip()
    url = f"http://{ip}:{PORT}/{PASSWORD}"
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


# ================= 删除/清屏 API =================
@app.delete("/{p}/api/msg/{msg_id}")
async def delete_msg(p: str, msg_id: str):
    check_password(p)

    target = None
    new_hist = []
    for m in state["history"]:
        if m.get("id") == msg_id:
            target = m
        else:
            new_hist.append(m)

    if target is None:
        return JSONResponse({"ok": True, "deleted": 0, "files_deleted": 0})

    files_deleted = delete_attachments_files(target.get("attachments") or [])

    state["history"] = new_hist
    rewrite_history_file()

    await broadcast({"type": "delete", "id": msg_id})
    return JSONResponse({"ok": True, "deleted": 1, "files_deleted": files_deleted})

@app.post("/{p}/api/clear")
async def clear_all(p: str):
    check_password(p)

    state["history"] = []
    rewrite_history_file()

    files_deleted = purge_uploads_all()

    await broadcast({"type": "clear"})
    return JSONResponse({"ok": True, "files_deleted": files_deleted})


# ================= 上传接口 =================
@app.post("/{p}/upload")
async def upload(p: str, pass_: str = Form(..., alias="pass"), up: UploadFile = File(...)):
    check_password(p)
    if pass_ != PASSWORD:
        raise HTTPException(status_code=403, detail="Forbidden")

    original = up.filename or "file"
    ext = safe_ext(original)

    fid = uuid.uuid4().hex

    day = time.strftime("%Y-%m-%d", time.localtime())
    day_dir = UPLOAD_DIR / day
    day_dir.mkdir(parents=True, exist_ok=True)

    save_name = f"{fid}{ext}"
    save_path = day_dir / save_name

    total = 0
    with open(save_path, "wb") as f:
        while True:
            chunk = await up.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            f.write(chunk)

    kind = "image" if is_image(original) else "file"
    url = f"/uploads/{day}/{save_name}"
    return {"url": url, "name": original, "size": total, "kind": kind}


# ================= WebSocket =================
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, pass_: str):
    if pass_ != PASSWORD:
        await ws.close(code=1008)
        return

    await ws.accept()
    state["clients"].add(ws)

    await ws.send_text(json.dumps({"type":"history", "items": state["history"]}, ensure_ascii=False))

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            if msg.get("type") != "msg":
                continue
            if msg.get("pass") != PASSWORD:
                await ws.send_text(json.dumps({"type":"error","message":"Forbidden"}, ensure_ascii=False))
                continue

            item = {
                "id": uuid.uuid4().hex,
                "ts": now_ms(),
                "sender": str(msg.get("sender",""))[:30],
                "text": str(msg.get("text",""))[:5000],
                "attachments": msg.get("attachments") or [],
            }

            # 只允许 /uploads/，并校验 size（<= MAX_FILE_MB）
            clean = []
            max_bytes = (MAX_FILE_MB * 1024 * 1024) if isinstance(MAX_FILE_MB, (int, float)) and MAX_FILE_MB else None

            for a in item["attachments"]:
                try:
                    url = normalize_upload_url(str(a.get("url", "")))
                    name = str(a.get("name", "file"))[:120]
                    size = int(a.get("size", 0))
                    kind = str(a.get("kind", "file"))

                    if not url.startswith("/uploads/"):
                        continue
                    if size < 0:
                        continue
                    if max_bytes is not None and size > max_bytes:
                        continue
                    if kind not in ("image", "file"):
                        kind = "file"

                    clean.append({"url": url, "name": name, "size": size, "kind": kind})
                except Exception:
                    pass

            item["attachments"] = clean

            append_history(item)
            await broadcast({"type":"msg", "item": item})

    except WebSocketDisconnect:
        state["clients"].discard(ws)
    except Exception:
        state["clients"].discard(ws)


# ================= 托盘 =================
from PIL import Image, ImageDraw

def make_icon() -> Image.Image:
    # 优先加载自定义图标：根目录 lanboard.ico，其次 assets/lanboard.ico（兼容你当前仓库结构）
    try:
        for rel in ("lanboard.ico", "assets/lanboard.ico"):
            p = resource_path(rel)
            if p.exists():
                return Image.open(p)
        raise FileNotFoundError("lanboard.ico not found")
    except Exception:
        # fallback：运行环境缺少 ico 或 Pillow 无法读取时，用程序生成一个简单图标
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((8, 8, 56, 56), radius=12, fill=(40, 120, 255, 255))
        d.text((18, 18), "LB", fill=(255, 255, 255, 255))
        return img

def run_server_in_thread() -> uvicorn.Server:
    load_history()
    config = uvicorn.Config(
        app, 
        host=HOST, 
        port=PORT, 
        log_config=None, 
        access_log=False, 
        # log_level="warning", 
        ws="websockets"
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return server

def start_tray(server: uvicorn.Server):
    ip = get_lan_ip()
    url = f"http://{ip}:{PORT}/{PASSWORD}"

    def open_ui(icon, item):
        webbrowser.open(url)

    def open_uploads(icon, item):
        try:
            os.startfile(str(UPLOAD_DIR))
        except Exception as e:
            print(f"open uploads failed: {e}")

    def quit_app(icon, item):
        server.should_exit = True
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开页面", open_ui),
        pystray.MenuItem("打开 uploads 目录", open_uploads),
        pystray.MenuItem("退出", quit_app),
    )

    tip = tray_tip(f"{APP_NAME} v{APP_VERSION}")
    icon = pystray.Icon(APP_NAME, make_icon(), tip, menu)
    icon.run()


# ================= 启动 =================
if __name__ == "__main__":
    # 统一 cwd，避免 System32 坑
    os.chdir(str(BASE_DIR))

    # 自检（端口/目录）
    self_check_or_die()

    cleanup_uploads_once()
    server = run_server_in_thread()
    start_tray(server)
