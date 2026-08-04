#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
docker/wehub.py — 网页版 Hub：一键启动/停止 GenericAgent 各类客户端（容器场景）

用法:
    python docker/wehub.py [--host 0.0.0.0] [--port 8901] [--token xxx]

功能:
    - 自动发现: frontends/*app*.py (bot/UI) + reflect/*.py (反射服务)
    - 依赖检测: AST 扫描 import, 缺包标记「缺依赖」不可启动
    - 一键启动/停止/日志尾部/状态轮询 (端口探测 + 进程存活)
    - 轻量: 仅依赖 bottle (GA 核心依赖自带)

环境变量: WEHUB_HOST / WEHUB_PORT / WEHUB_TOKEN (优先级: 命令行参数 > 环境变量)
"""
import os, sys, json, time, ast, signal, socket, threading, subprocess, importlib.util
from collections import deque
from bottle import route, get, post, request, response, run

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根 (/app)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
LOG_DIR = os.path.join(ROOT, "temp", "wehub_logs")

HOST = os.environ.get("WEHUB_HOST", "0.0.0.0")
PORT = int(os.environ.get("WEHUB_PORT", "8901"))
TOKEN = os.environ.get("WEHUB_TOKEN", "")
for i, a in enumerate(sys.argv):
    if a == "--host" and i + 1 < len(sys.argv): HOST = sys.argv[i + 1]
    if a == "--port" and i + 1 < len(sys.argv): PORT = int(sys.argv[i + 1])
    if a == "--token" and i + 1 < len(sys.argv): TOKEN = sys.argv[i + 1]

# ── 服务类别 ──────────────────────────────────────────────
CATEGORY = {
    "stapp.py": "Web UI", "stapp2.py": "Web UI", "conductor": "Web UI",
    "tgapp.py": "IM Bot", "dcapp.py": "IM Bot", "qqapp.py": "IM Bot",
    "fsapp.py": "IM Bot", "dingtalkapp.py": "IM Bot",
    "wecomapp.py": "IM Bot", "wechatapp.py": "IM Bot",
}
DESKTOP_ONLY = {"qtapp.py", "tuiapp.py", "tuiapp_v2.py"}  # 需桌面/交互终端, 容器不可用
EXCLUDE = {"chatapp_common.py"}                            # 公共库, 非独立服务
FIXED_PORTS = {"stapp.py": 8501, "stapp2.py": 8502, "conductor": 8900}


def scan_imports(path):
    """AST 扫描模块的所有 import 顶层包名 (不执行代码, 安全)"""
    mods = set()
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except SyntaxError:
        return mods
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                mods.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def mod_available(name):
    # frontends/ 下的本地模块可被 stapp/tgapp 等导入
    if os.path.isdir(os.path.join(ROOT, "frontends")):
        sys.path.append(os.path.join(ROOT, "frontends"))
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def port_open(port, host="127.0.0.1", timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover_services():
    svcs = {}
    # 固定项: Streamlit 主 UI
    svcs["stapp.py"] = {
        "name": "Streamlit 主界面", "cat": "Web UI", "file": "frontends/stapp.py",
        "cmd": [sys.executable, "-m", "streamlit", "run", "frontends/stapp.py",
                "--server.port", "8501", "--server.address", "0.0.0.0",
                "--server.headless", "true"],
        "port": 8501, "kind": "fixed",
    }
    # frontends/*app*.py
    fe = os.path.join(ROOT, "frontends")
    for f in sorted(os.listdir(fe)):
        if not (f.endswith(".py") and "app" in f and not f.startswith("_")):
            continue
        if f in EXCLUDE:
            continue
        if f in DESKTOP_ONLY:
            svcs[f] = {"name": f, "cat": "Desktop", "file": f"frontends/{f}",
                       "cmd": None, "port": None, "kind": "desktop"}
            continue
        if f == "stapp.py":
            continue  # 固定项已在上方注册(streamlit run 8501), 避免被覆盖成裸脚本
        if "stapp" in f:  # streamlit 应用必须用 streamlit run 启动, 裸 python 会跑完即退出
            cmd = [sys.executable, "-m", "streamlit", "run", f"frontends/{f}",
                   "--server.port", str(FIXED_PORTS.get(f, 8502)),
                   "--server.address", "0.0.0.0", "--server.headless", "true"]
        else:
            cmd = [sys.executable, f"frontends/{f}"]
        svcs[f] = {
            "name": f.replace(".py", ""), "cat": CATEGORY.get(f, "App"),
            "file": f"frontends/{f}",
            "cmd": cmd,
            "port": FIXED_PORTS.get(f), "kind": "frontend",
        }
    # reflect/*.py (agentmain --reflect)
    rf = os.path.join(ROOT, "reflect")
    if os.path.isdir(rf):
        for f in sorted(os.listdir(rf)):
            if f.endswith(".py") and not f.startswith("_"):
                svcs[f"reflect/{f}"] = {
                    "name": f"reflect/{f}", "cat": "Reflect",
                    "file": f"reflect/{f}",
                    "cmd": [sys.executable, "agentmain.py", "--reflect", f"reflect/{f}"],
                    "port": None, "kind": "reflect",
                }
    return svcs


# ── 进程管理 ──────────────────────────────────────────────
procs = {}      # key -> dict(proc, log deque, logfile, started)
LOCK = threading.Lock()


def _reader(key, proc, buf, fh):
    try:
        for line in proc.stdout:
            buf.append(line)
            fh.write(line)
            fh.flush()
    except Exception:
        pass
    finally:
        try:
            fh.close()
        except Exception:
            pass


def start_service(key, svc):
    with LOCK:
        p = procs.get(key)
        if p and p["proc"].poll() is None:
            return {"ok": False, "msg": "已在运行"}
        if svc.get("cmd") is None:
            return {"ok": False, "msg": "该服务在此环境不可启动"}
        os.makedirs(LOG_DIR, exist_ok=True)
        logfile = os.path.join(LOG_DIR, key.replace("/", "_") + ".log")
        fh = open(logfile, "a", encoding="utf-8", errors="replace")
        buf = deque(maxlen=500)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["GA_LANG"] = env.get("GA_LANG", "zh")
        proc = subprocess.Popen(svc["cmd"], cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                env=env)
        procs[key] = {"proc": proc, "buf": buf, "logfile": logfile,
                      "started": time.time()}
        threading.Thread(target=_reader, args=(key, proc, buf, fh), daemon=True).start()
        return {"ok": True, "msg": f"已启动 PID {proc.pid}"}


def stop_service(key):
    with LOCK:
        p = procs.get(key)
        if not p or p["proc"].poll() is not None:
            return {"ok": False, "msg": "未在运行"}
        proc = p["proc"]
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        return {"ok": True, "msg": "已停止"}


def service_state(key, svc):
    p = procs.get(key)
    running = bool(p and p["proc"].poll() is None)
    if not running and p:
        procs.pop(key, None)
    st = {"key": key, "name": svc["name"], "cat": svc["cat"], "file": svc["file"],
          "port": svc.get("port"), "kind": svc.get("kind"),
          "running": running, "pid": p["proc"].pid if running and p else None,
          "port_open": bool(svc.get("port") and port_open(svc["port"])),
          "missing_deps": [], "startable": True}
    if svc.get("kind") == "desktop":
        st["startable"] = False
        st["reason"] = "需桌面/交互终端, 容器不可用"
        return st
    # 依赖检测 (AST)
    path = os.path.join(ROOT, svc["file"])
    if os.path.isfile(path):
        std_ok = {"os", "sys", "json", "time", "threading", "subprocess", "re",
                  "random", "logging", "argparse", "pathlib", "asyncio", "typing",
                  "collections", "queue", "dataclasses", "urllib", "socket", "io",
                  "base64", "hashlib", "traceback", "functools", "itertools",
                  "textwrap", "signal", "select", "importlib", "platform",
                  "shutil", "tempfile", "glob", "string", "copy", "abc",
                  "contextlib", "weakref", "http", "ssl", "email", "uuid",
                  "warnings", "html", "csv", "math", "statistics", "decimal",
                  "fractions", "numbers", "operator", "bisect", "heapq", "array",
                  "types", "enum", "codecs", "locale", "gettext", "code",
                  "compileall", "dis", "inspect", "ast", "tokenize", "keyword",
                  "pickle", "shelve", "marshal", "dbm", "sqlite3", "xml",
                  "wsgiref", "imaplib", "poplib", "smtplib", "ftplib", "telnetlib",
                  "mailbox", "mimetools", "mimetypes", "mimelib", "rfc822", "uu",
                  "xdrlib", "binhex", "pipes", "commands", "builtins", "__future__",
                  "ctypes", "curses", "errno", "faulthandler", "fcntl", "filecmp",
                  "fileinput", "fnmatch", "gc", "getopt", "getpass", "graphlib",
                  "grp", "hmac", "ipaddress", "itertools", "linecache", "lzma",
                  "mmap", "modulefinder", "msilib", "msvcrt", "multiprocessing",
                  "netrc", "nis", "nntplib", "numbers", "optparse", "ossaudiodev",
                  "parser", "pdb", "pickletools", "pkgutil", "platform", "plistlib",
                  "posix", "posixpath", "pprint", "profile", "pstats", "pty",
                  "pwd", "py_compile", "pyclbr", "pydoc", "queue", "quopri",
                  "readline", "reprlib", "resource", "rlcompleter", "runpy",
                  "sched", "secrets", "shlex", "shutil", "site", "sndhdr",
                  "socketserver", "sre_compile", "sre_constants", "sre_parse",
                  "stat", "stringprep", "struct", "sunau", "symbol", "symtable",
                  "sysconfig", "syslog", "tabnanny", "termios", "token",
                  "tokenize", "trace", "tracemalloc", "tty", "turtle", "unicodedata",
                  "unittest", "uu", "venv", "wave", "webbrowser", "winreg",
                  "winsound", "zipapp", "zoneinfo"}
        std_ok |= {"agentmain", "llmcore", "ga", "agent_loop", "simphtml",
                   "TMWebDriver", "bottle", "simple_websocket", "requests",
                   "bs4", "aiohttp", "prompt_toolkit", "rich", "PIL",
                   "streamlit", "ga_cli", "chatapp_common"}
        for m in sorted(scan_imports(path) - std_ok):
            if not mod_available(m):
                st["missing_deps"].append(m)
    if st["missing_deps"]:
        st["startable"] = False
        st["reason"] = "缺依赖: " + ", ".join(st["missing_deps"])
    return st


# ── API ──────────────────────────────────────────────────
def check_token():
    if not TOKEN:
        return True
    return request.get_header("X-WEHUB-TOKEN") == TOKEN


@get("/api/services")
def api_services():
    if not check_token():
        response.status = 401
        return {"ok": False, "msg": "unauthorized"}
    svcs = discover_services()
    return {"ok": True, "services": [service_state(k, v) for k, v in svcs.items()]}


@post("/api/start/<key>")
def api_start(key):
    if not check_token():
        response.status = 401
        return {"ok": False, "msg": "unauthorized"}
    svc = discover_services().get(key)
    if not svc:
        return {"ok": False, "msg": "服务不存在"}
    return start_service(key, svc)


@post("/api/stop/<key>")
def api_stop(key):
    if not check_token():
        response.status = 401
        return {"ok": False, "msg": "unauthorized"}
    return stop_service(key)


@get("/api/log/<key>")
def api_log(key):
    if not check_token():
        response.status = 401
        return {"ok": False, "msg": "unauthorized"}
    p = procs.get(key)
    lines = list(p["buf"]) if p else []
    return {"ok": True, "lines": lines}


@get("/")
def index():
    if not check_token():
        response.status = 401
        return HTML.replace("__TOKEN__", json.dumps(""))
    return HTML.replace("__TOKEN__", json.dumps(TOKEN))


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GA Hub (Web)</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--line:#334155;--txt:#e2e8f0;--mut:#94a3b8;
--ok:#22c55e;--run:#22c55e;--off:#64748b;--bad:#ef4444;--amber:#f59e0b;--acc:#3b82f6}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif;padding:20px}
h1{font-size:20px;margin-bottom:16px;display:flex;align-items:center;gap:10px}
h1 .dot{width:10px;height:10px;border-radius:50%;background:var(--ok);animation:blink 2s infinite}
@keyframes blink{50%{opacity:.3}}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.card .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.card .name{font-weight:600}
.tag{font-size:11px;padding:2px 8px;border-radius:99px;background:#334155;color:var(--mut)}
.st{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.st.run{background:var(--run);box-shadow:0 0 8px var(--run)}
.st.off{background:var(--off)}.st.bad{background:var(--bad)}
.card .meta{color:var(--mut);font-size:12px;margin-bottom:10px;word-break:break-all}
.btns{display:flex;gap:8px}
button{border:0;border-radius:8px;padding:6px 14px;font-size:13px;cursor:pointer;color:#fff}
.b-start{background:var(--acc)}.b-stop{background:var(--bad)}
.b-dis{background:#334155;cursor:not-allowed}
.logs{margin-top:10px;background:#0b1220;border:1px solid var(--line);border-radius:8px;
padding:8px;max-height:180px;overflow:auto;font:11px/1.45 ui-monospace,Consolas,monospace;
color:#cbd5e1;white-space:pre-wrap;display:none}
.port{padding:2px 8px;border-radius:6px;font-size:11px}
.p-open{background:rgba(34,197,94,.15);color:var(--ok)}
.p-closed{background:rgba(100,116,139,.15);color:var(--mut)}
.reason{color:var(--amber);font-size:12px;margin-top:6px}
</style>
</head>
<body>
<h1><span class="dot"></span>GA Hub · 一键启动各类客户端</h1>
<div class="grid" id="grid"></div>
<script>
const TOKEN = __TOKEN__ || new URLSearchParams(location.search).get('token') || localStorage.getItem('wehub_token');
if (TOKEN) localStorage.setItem('wehub_token', TOKEN);
const hdr = TOKEN ? {"X-WEHUB-TOKEN": TOKEN} : {};
async function api(path, opt={}){const r=await fetch(path,{...opt,headers:{...hdr,...(opt.headers||{})}});
if(r.status===401){document.body.innerHTML='<h1 style="padding:40px">需要 WEHUB_TOKEN: 访问 /?token=xxx 或请求头 X-WEHUB-TOKEN</h1>';throw 401}
return r.json();}
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function act(key, what){
  await api('/api/'+what+'/'+encodeURIComponent(key),{method:'POST'});
  refresh();
}
async function toggleLog(key, el){
  const box=document.getElementById('log-'+key);
  if(box.style.display==='block'){box.style.display='none';return;}
  const d=await api('/api/log/'+encodeURIComponent(key));
  box.textContent=d.lines.join('')||'(无日志)';
  box.style.display='block';box.scrollTop=box.scrollHeight;
}
function card(s){
  const st=s.running?'run':(s.port_open?'run':'off');
  const stTxt=s.running?('运行中'+(s.pid?' PID '+s.pid:'')):(s.port_open?'端口已开':'已停止');
  const portHtml=s.port?`<span class="port ${s.port_open?'p-open':'p-closed'}">:${s.port} ${s.port_open?'●':'○'}</span>`:'';
  const btn=s.startable
    ? (s.running
       ? `<button class="b-stop" onclick="act('${s.key}','stop')">停止</button>`
       : `<button class="b-start" onclick="act('${s.key}','start')">启动</button>`)
    : `<button class="b-dis" disabled>${s.reason||'不可用'}</button>`;
  const missing=s.missing_deps.length?`<div class="reason">缺依赖: ${esc(s.missing_deps.join(', '))}</div>`:'';
  return `<div class="card">
    <div class="top"><span class="name">${esc(s.name)}</span>
      <span><span class="tag">${esc(s.cat)}</span> ${portHtml}</span></div>
    <div class="meta">${esc(s.file)}<br><span class="st ${st}"></span>${stTxt}</div>
    <div class="btns">${btn}<button class="b-dis" onclick="toggleLog('${s.key}',this)">日志</button></div>
    <div class="logs" id="log-${s.key}"></div>${missing}</div>`;
}
async function refresh(){
  try{const d=await api('/api/services');
  document.getElementById('grid').innerHTML=d.services.map(card).join('');}
  catch(e){}
}
refresh();
setInterval(refresh, 3000);
</script>
</body></html>
"""

if __name__ == "__main__":
    os.makedirs(LOG_DIR, exist_ok=True)
    if not TOKEN:
        print("[WEHUB] ⚠️ 未设置 WEHUB_TOKEN —— 面板无鉴权，任何能访问本端口的人可操作你的客户端！")
        print("[WEHUB]    建议: docker compose 目录 .env 写入 WEHUB_TOKEN=口令 后重建, 访问 http://HOST:8901/?token=口令")
    print(f"[WEHUB] GA Web Hub on http://{HOST}:{PORT}  (token: {'set' if TOKEN else 'none'})  root={ROOT}")
    run(host=HOST, port=PORT, quiet=True)
