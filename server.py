#!/usr/bin/env python3
"""帕鲁特金矿智能运维平台服务。

支持本机开发运行；生产环境通过 Railway 等 HTTPS 反向代理提供公网访问，
必须配置 APP_PASSWORD、SESSION_SECRET，并将数据库和 API 配置放入持久化目录。
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
import argparse
import base64
import json
import os
import sqlite3
import threading
import mimetypes
import io
import urllib.request
import urllib.error
import re
import hmac
import hashlib
import secrets
import time
from http.cookies import SimpleCookie

BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / 'index.html'
DB_FILE = Path(os.environ.get('INVENTORY_DB', str(BASE_DIR / 'inventory_shared.db')))
ARK_CONFIG_FILE = Path(os.environ.get('ARK_CONFIG_FILE', str(BASE_DIR / 'ark_config.json')))
ARK_API_URL = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
DB_LOCK = threading.RLock()
APP_ENV = os.environ.get('APP_ENV', 'local').lower()
APP_USERNAME = os.environ.get('APP_USERNAME', 'admin').strip() or 'admin'
APP_PASSWORD = os.environ.get('APP_PASSWORD', '')
SESSION_SECRET = os.environ.get('SESSION_SECRET', '')
SESSION_TTL_SECONDS = 12 * 60 * 60
AUTH_ENABLED = bool(APP_PASSWORD)
LOGIN_LOCK = threading.Lock()
LOGIN_FAILURES = {}
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 8


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def connect():
    conn = sqlite3.connect(DB_FILE, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARK_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS inventory_items (
            item_key TEXT PRIMARY KEY,
            factory_key TEXT NOT NULL DEFAULT '', factory_name TEXT NOT NULL DEFAULT '',
            workshop_key TEXT NOT NULL DEFAULT '', workshop_name TEXT NOT NULL DEFAULT '',
            equipment_name TEXT NOT NULL DEFAULT '', bundle_id TEXT NOT NULL DEFAULT '',
            bundle_name TEXT NOT NULL DEFAULT '', part_name TEXT NOT NULL DEFAULT '',
            part_code TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '件',
            quantity REAL, min_stock REAL, location TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT ''
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS inventory_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_key TEXT NOT NULL,
            factory_name TEXT NOT NULL DEFAULT '', workshop_name TEXT NOT NULL DEFAULT '',
            equipment_name TEXT NOT NULL DEFAULT '', part_name TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL, quantity REAL NOT NULL,
            before_qty REAL, after_qty REAL,
            operator TEXT NOT NULL DEFAULT '', batch TEXT NOT NULL DEFAULT '',
            supplier TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_transactions_time ON inventory_transactions(created_at DESC)')


def normalize_item(raw):
    raw = raw or {}
    def pick(*keys, default=''):
        for key in keys:
            value = raw.get(key)
            if value is not None and value != '':
                return value
        return default
    item = {
        'item_key': str(pick('item_key', 'key')),
        'factory_key': str(pick('factory_key', 'factoryKey')),
        'factory_name': str(pick('factory_name', 'factoryName')),
        'workshop_key': str(pick('workshop_key', 'workshopKey')),
        'workshop_name': str(pick('workshop_name', 'workshopName')),
        'equipment_name': str(pick('equipment_name', 'equipmentName')),
        'bundle_id': str(pick('bundle_id', 'bundleId', default='custom')),
        'bundle_name': str(pick('bundle_name', 'bundleName', default='自定义配件')),
        'part_name': str(pick('part_name', 'partName')),
        'part_code': str(pick('part_code', 'partCode')),
        'unit': str(pick('unit', default='件')),
        'location': str(pick('location')),
    }
    if not item['item_key']:
        parts = [item['factory_key'], item['workshop_key'], item['equipment_name'], item['bundle_id'], item['part_name']]
        from urllib.parse import quote
        item['item_key'] = '|'.join(quote(str(x), safe='') for x in parts)
    missing = [k for k in ('factory_name', 'workshop_name', 'equipment_name', 'part_name') if not item[k]]
    if missing:
        raise ValueError('库存档案字段不完整：' + ', '.join(missing))
    return item


def apply_transaction(conn, payload):
    item = normalize_item(payload.get('item'))
    action = str(payload.get('action', '')).strip()
    if action not in ('期初盘点', '盘点', '入库', '出库'):
        raise ValueError('无效的库存操作类型。')
    try:
        amount = float(payload.get('quantity'))
    except (TypeError, ValueError):
        raise ValueError('数量必须为数字。')
    if amount < 0:
        raise ValueError('数量不能为负数。')
    operator = str(payload.get('operator', '')).strip()
    if not operator:
        raise ValueError('请填写操作人。')
    if len(operator) > 100:
        raise ValueError('操作人字段过长。')

    row = conn.execute('SELECT quantity,min_stock FROM inventory_items WHERE item_key=?', (item['item_key'],)).fetchone()
    before = row['quantity'] if row else None
    if action in ('期初盘点', '盘点'):
        after = amount
    elif action == '入库':
        if before is None:
            raise ValueError('该配件尚无期初库存，请先盘点再登记入库。')
        after = float(before) + amount
    else:  # 出库
        if before is None:
            raise ValueError('该配件尚无期初库存，请先盘点再登记出库。')
        if amount > float(before):
            raise ValueError('出库数量大于当前库存，已阻止负库存。')
        after = float(before) - amount

    min_stock_raw = payload.get('minStock', payload.get('min_stock', None))
    if min_stock_raw in ('', None):
        min_stock = row['min_stock'] if row else None
    else:
        try:
            min_stock = float(min_stock_raw)
        except (TypeError, ValueError):
            raise ValueError('最低库存必须为数字或留空。')
        if min_stock < 0:
            raise ValueError('最低库存不能为负数。')

    created = now_iso()
    conn.execute('''INSERT INTO inventory_items
        (item_key,factory_key,factory_name,workshop_key,workshop_name,equipment_name,bundle_id,bundle_name,
         part_name,part_code,unit,quantity,min_stock,location,updated_at,updated_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(item_key) DO UPDATE SET
         factory_key=excluded.factory_key,factory_name=excluded.factory_name,
         workshop_key=excluded.workshop_key,workshop_name=excluded.workshop_name,
         equipment_name=excluded.equipment_name,bundle_id=excluded.bundle_id,bundle_name=excluded.bundle_name,
         part_name=excluded.part_name,part_code=excluded.part_code,unit=excluded.unit,
         quantity=excluded.quantity,min_stock=excluded.min_stock,location=excluded.location,
         updated_at=excluded.updated_at,updated_by=excluded.updated_by''',
         (item['item_key'], item['factory_key'], item['factory_name'], item['workshop_key'], item['workshop_name'],
          item['equipment_name'], item['bundle_id'], item['bundle_name'], item['part_name'], item['part_code'],
          item['unit'], after, min_stock, item['location'], created, operator))
    conn.execute('''INSERT INTO inventory_transactions
        (item_key,factory_name,workshop_name,equipment_name,part_name,action,quantity,before_qty,after_qty,
         operator,batch,supplier,note,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (item['item_key'], item['factory_name'], item['workshop_name'], item['equipment_name'], item['part_name'],
         action, amount, before, after, operator, str(payload.get('batch', '') or ''),
         str(payload.get('supplier', '') or ''), str(payload.get('note', '') or ''), created))
    return {'item_key': item['item_key'], 'before_qty': before, 'after_qty': after, 'created_at': created}


def rows_from_xlsx(file_bytes):
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise RuntimeError('服务器尚未安装 openpyxl，请运行：pip install -r requirements.txt')
    wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    values = list(ws.iter_rows(values_only=True))
    wb.close()
    if not values:
        return []
    headers = [str(v).strip() if v is not None else '' for v in values[0]]
    result = []
    for line in values[1:]:
        if not any(v is not None and str(v).strip() for v in line):
            continue
        result.append({headers[i]: ('' if i >= len(line) or line[i] is None else str(line[i]).strip())
                       for i in range(len(headers)) if headers[i]})
    return result


def load_ark_config():
    try:
        data = json.loads(ARK_CONFIG_FILE.read_text(encoding='utf-8'))
        return {'api_key': str(data.get('api_key','')).strip(), 'model': str(data.get('model','')).strip()}
    except Exception:
        return {'api_key':'','model':''}

def save_ark_config(api_key=None, model=None):
    old = load_ark_config()
    if api_key is not None and str(api_key).strip(): old['api_key'] = str(api_key).strip()
    if model is not None and str(model).strip(): old['model'] = str(model).strip()
    if not old['api_key'] or not old['model']:
        raise ValueError('请同时配置 API Key 和模型 ID / 推理接入点 ID。')
    ARK_CONFIG_FILE.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding='utf-8')
    try: os.chmod(ARK_CONFIG_FILE, 0o600)
    except Exception: pass
    return old

def inventory_context_for_ai(question):
    # 只从云端 SQLite 读取真实库存；没有记录时明确返回空，不生成库存数字。
    with DB_LOCK, connect() as conn:
        rows = conn.execute('''SELECT factory_name,workshop_name,equipment_name,part_name,part_code,unit,quantity,min_stock,location,updated_at,updated_by
                               FROM inventory_items ORDER BY updated_at DESC LIMIT 300''').fetchall()
    q = str(question).lower()
    words = [w.strip() for w in re.split(r'[\s，。；、,!?！？:：]+', q) if len(w.strip()) >= 2]
    # 中文连续文本也拆成相邻双字词，避免整句匹配不到配件名/设备名。
    compact = re.sub(r'[\s，。；、,!?！？:：]+', '', q)
    words.extend(compact[i:i+2] for i in range(max(0, len(compact)-1)) if re.search(r'[\u4e00-\u9fff]', compact[i:i+2]))
    words = list(dict.fromkeys(words))[:30]
    selected=[]
    for row in rows:
        d=dict(row); hay=' '.join(str(v or '') for v in d.values()).lower()
        if not words or any(w.lower() in hay for w in words): selected.append(d)
        if len(selected)>=40: break
    return selected

def call_doubao(messages, max_tokens=1800):
    cfg=load_ark_config()
    if not cfg['api_key'] or not cfg['model']:
        raise ValueError('尚未配置 AI 接口。请在 AI 助手页面点击“接口设置”，填写 API Key 和模型 ID / 推理接入点 ID。')
    payload={'model':cfg['model'],'messages':messages,'max_completion_tokens':max_tokens,'stream':False}
    req=urllib.request.Request(ARK_API_URL,data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg['api_key']},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=120) as resp: result=json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail=e.read().decode('utf-8','replace')[:1200]
        raise RuntimeError('火山方舟 API 返回 HTTP %s：%s' % (e.code, detail))
    except urllib.error.URLError as e:
        raise RuntimeError('无法连接火山方舟：%s' % e.reason)
    choices=result.get('choices') or []
    if not choices: raise RuntimeError('模型未返回 choices，请检查模型权限和接口响应。')
    content=choices[0].get('message',{}).get('content','')
    if isinstance(content,list): content='\n'.join(str(x.get('text','')) for x in content if isinstance(x,dict))
    return str(content), result.get('usage')


def auth_configuration_error():
    if APP_ENV == 'production':
        if not APP_PASSWORD or len(APP_PASSWORD) < 12:
            return '生产环境必须设置至少 12 位的 APP_PASSWORD。'
        if not SESSION_SECRET or len(SESSION_SECRET) < 32:
            return '生产环境必须设置至少 32 个字符的 SESSION_SECRET。'
    return ''


def _session_signature(payload_b64):
    return hmac.new(SESSION_SECRET.encode('utf-8'), payload_b64.encode('ascii'), hashlib.sha256).hexdigest()


def make_session_token():
    payload = json.dumps({'u': APP_USERNAME, 'e': int(time.time()) + SESSION_TTL_SECONDS,
                          'n': secrets.token_urlsafe(16)}, separators=(',', ':')).encode('utf-8')
    payload_b64 = base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')
    return payload_b64 + '.' + _session_signature(payload_b64)


def valid_session_token(token):
    if not AUTH_ENABLED or not SESSION_SECRET or not token or '.' not in token:
        return False
    payload_b64, signature = token.rsplit('.', 1)
    expected = _session_signature(payload_b64)
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        padded = payload_b64 + '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8'))
        return payload.get('u') == APP_USERNAME and int(payload.get('e', 0)) > int(time.time())
    except Exception:
        return False


def login_blocked(client_key):
    now = time.time()
    with LOGIN_LOCK:
        entry = LOGIN_FAILURES.get(client_key)
        if not entry or now - entry['start'] > LOGIN_WINDOW_SECONDS:
            LOGIN_FAILURES[client_key] = {'start': now, 'count': 0}
            return False
        return entry['count'] >= LOGIN_MAX_FAILURES


def record_login_failure(client_key):
    now = time.time()
    with LOGIN_LOCK:
        entry = LOGIN_FAILURES.get(client_key)
        if not entry or now - entry['start'] > LOGIN_WINDOW_SECONDS:
            LOGIN_FAILURES[client_key] = {'start': now, 'count': 1}
        else:
            entry['count'] += 1


def clear_login_failures(client_key):
    with LOGIN_LOCK:
        LOGIN_FAILURES.pop(client_key, None)



class Handler(BaseHTTPRequestHandler):
    server_version = 'PalutInventory/1.1'

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        super().end_headers()

    def is_authenticated(self):
        if not AUTH_ENABLED:
            return True
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            token = cookie.get('palut_session')
            return bool(token and valid_session_token(token.value))
        except Exception:
            return False

    def client_key(self):
        # Use the direct peer address; do not trust arbitrary forwarded headers for authentication.
        return self.client_address[0] if self.client_address else 'unknown'

    def send_redirect(self, location):
        self.send_response(303)
        self.send_header('Location', location)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def send_login_page(self, message='', status=200):
        safe_message = (message.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                        .replace('"', '&quot;').replace("'", '&#39;'))
        body = ('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<title>帕鲁特金矿智能运维平台 - 登录</title><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f2f5f8;font-family:Arial,"Microsoft YaHei",sans-serif;color:#183047}.card{width:min(92vw,390px);background:#fff;padding:30px;border:1px solid #e1e8ef;border-radius:16px;box-shadow:0 14px 40px #1c355018}h1{font-size:21px;margin:0 0 8px}.sub{color:#687b8e;font-size:13px;line-height:1.7;margin-bottom:24px}label{display:block;font-size:13px;font-weight:600;margin:14px 0 7px}input{width:100%;padding:12px;border:1px solid #cbd5df;border-radius:8px;font-size:15px}button{width:100%;margin-top:22px;padding:12px;border:0;border-radius:8px;background:#087f78;color:white;font-size:15px;cursor:pointer}.msg{color:#b42318;background:#fff1f0;padding:9px 11px;border-radius:7px;font-size:13px;margin:12px 0}</style></head><body><main class="card"><h1>帕鲁特金矿 · 智能运维平台</h1><div class="sub">请输入访问账号和密码。仅授权人员可以查看设备资料、库存记录和 AI 助手。</div>''' +
                (('<div class="msg">' + safe_message + '</div>') if safe_message else '') +
                '''<form method="post" action="/login" autocomplete="on"><label for="username">账号</label><input id="username" name="username" autocomplete="username" required maxlength="80"><label for="password">密码</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="256"><button type="submit">登录平台</button></form></main></body></html>''').encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def set_session_cookie(self):
        token = make_session_token()
        secure = APP_ENV == 'production' or self.headers.get('X-Forwarded-Proto', '').lower() == 'https'
        cookie = 'palut_session=' + token + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=' + str(SESSION_TTL_SECONDS)
        if secure:
            cookie += '; Secure'
        self.send_header('Set-Cookie', cookie)

    def log_message(self, fmt, *args):
        print('[%s] %s' % (self.log_date_time_string(), fmt % args))

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length <= 0 or length > 30 * 1024 * 1024:
            raise ValueError('请求内容为空或超过30MB限制。')
        return json.loads(self.rfile.read(length).decode('utf-8'))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/health':
            return self.send_json(200, {'ok': True})
        if path == '/login':
            if self.is_authenticated():
                return self.send_redirect('/')
            return self.send_login_page()
        if not self.is_authenticated():
            if path.startswith('/api/'):
                return self.send_json(401, {'error': '登录已过期，请重新登录。'})
            return self.send_redirect('/login')
        if path == '/api/settings':
            cfg=load_ark_config()
            return self.send_json(200, {'configured': bool(cfg['api_key'] and cfg['model']), 'model': cfg['model'], 'keyStored': bool(cfg['api_key'])})
        if path == '/api/inventory':
            with DB_LOCK, connect() as conn:
                items = [dict(r) for r in conn.execute('SELECT * FROM inventory_items ORDER BY factory_name,workshop_name,equipment_name,part_name')]
                transactions = [dict(r) for r in conn.execute('SELECT * FROM inventory_transactions ORDER BY id DESC LIMIT 1000')]
            return self.send_json(200, {'items': items, 'transactions': transactions, 'serverTime': now_iso()})
        if path in ('/', '/index.html'):
            if not HTML_FILE.exists():
                return self.send_error(404, 'HTML page not found')
            body = HTML_FILE.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return self.wfile.write(body)
        return self.send_error(404, 'Not found')

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/login':
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0 or length > 8192:
                    return self.send_login_page('登录请求无效，请重试。', 400)
                form = parse_qs(self.rfile.read(length).decode('utf-8', 'replace'), keep_blank_values=True)
                username = (form.get('username') or [''])[0].strip()
                password = (form.get('password') or [''])[0]
                client = self.client_key()
                if login_blocked(client):
                    return self.send_login_page('尝试次数过多，请 15 分钟后再试。', 429)
                if AUTH_ENABLED and hmac.compare_digest(username, APP_USERNAME) and hmac.compare_digest(password, APP_PASSWORD):
                    clear_login_failures(client)
                    self.send_response(303)
                    self.send_header('Location', '/')
                    self.send_header('Cache-Control', 'no-store')
                    self.set_session_cookie()
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                record_login_failure(client)
                return self.send_login_page('账号或密码不正确。', 401)
            except Exception:
                return self.send_login_page('登录失败，请检查输入后重试。', 400)
        if path == '/logout':
            self.send_response(303)
            self.send_header('Location', '/login')
            self.send_header('Set-Cookie', 'palut_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if not self.is_authenticated():
            return self.send_json(401, {'error': '登录已过期，请重新登录。'})
        try:
            if path == '/api/ai/test':
                answer, usage = call_doubao([{'role':'user','content':'请只回复：连接成功'}], max_tokens=32)
                return self.send_json(200, {'ok':True,'message':answer,'usage':usage})
            data = self.read_json()
            if path == '/api/settings':
                cfg=save_ark_config(data.get('apiKey'), data.get('model'))
                return self.send_json(200, {'ok':True,'model':cfg['model']})
            if path == '/api/ai/chat':
                question=str(data.get('question','')).strip()
                if not question and not data.get('imageData'): raise ValueError('请输入问题或附加一张图片。')
                if len(question)>12000: raise ValueError('问题过长，请控制在12000字以内。')
                context=data.get('deviceContext') or []
                if not isinstance(context,list): context=[]
                context=json.dumps(context[:5],ensure_ascii=False)[:24000]
                stock=inventory_context_for_ai(question)
                system=('你是帕鲁特金矿 AI 助手。回答中文，结构清晰、务实。必须严格区分：1)设备台账/用户资料事实；2)手册通用知识；3)AI推断。'
                        '没有明确资料就说“当前资料未提供/无法确认”，不得编造型号参数、库存数量、BOM、报警阈值、厂家限值或现场规程。'
                        '库存只能引用下方云端SQLite查询结果；空结果表示未查到，不能推测有货。'
                        '故障分析给出可能原因、优先检查项、需要补充的现场数据和安全边界；不要给出绕过联锁、带电操作、短接保护、井下违规操作的指令。'
                        '高压、井下、防爆、起重、受限空间和危险能量作业必须提醒遵守矿方批准规程并由授权人员执行。'
                        '如有图片，只描述可见内容和不确定性，不要仅凭照片认定内部故障。')
                inv=json.dumps(stock,ensure_ascii=False)[:18000]
                user_text='用户问题：'+question+'\n\n匹配到的设备手册片段（可能为空）：'+context+'\n\n本地库存数据库查询结果（仅此为库存事实）：'+inv
                content=[{'type':'text','text':user_text}]
                image_data=data.get('imageData')
                if image_data:
                    if not isinstance(image_data,str) or not image_data.startswith('data:image/') or len(image_data)>12*1024*1024:
                        raise ValueError('图片格式不正确或过大，请选择8MB以内的PNG/JPG/WEBP图片。')
                    content.append({'type':'image_url','image_url':{'url':image_data}})
                messages=[{'role':'system','content':system}]
                hist=data.get('history') or []
                for item in hist[-8:]:
                    if isinstance(item,dict) and item.get('role') in ('user','assistant'):
                        messages.append({'role':item['role'],'content':str(item.get('content',''))[:5000]})
                messages.append({'role':'user','content':content})
                answer,usage=call_doubao(messages)
                return self.send_json(200, {'ok':True,'answer':answer,'usage':usage,'inventoryMatches':len(stock)})
            if path == '/api/import-backup':
                backup = data.get('backup') or {}
                items = backup.get('items') or []
                transactions = backup.get('transactions') or []
                if not isinstance(items, list) or not isinstance(transactions, list):
                    raise ValueError('备份文件格式不正确。')
                if len(items) > 100000 or len(transactions) > 200000:
                    raise ValueError('备份记录数量超出限制。')
                with DB_LOCK, connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    existing_items = conn.execute('SELECT COUNT(*) FROM inventory_items').fetchone()[0]
                    existing_transactions = conn.execute('SELECT COUNT(*) FROM inventory_transactions').fetchone()[0]
                    if existing_items or existing_transactions:
                        raise ValueError('云端数据库已有库存记录。为防止重复导入，请先确认数据库为空或先备份。')
                    for item in items:
                        if not isinstance(item, dict) or not item.get('item_key'):
                            continue
                        conn.execute('''INSERT OR REPLACE INTO inventory_items
                            (item_key,factory_key,factory_name,workshop_key,workshop_name,equipment_name,bundle_id,bundle_name,
                             part_name,part_code,unit,quantity,min_stock,location,updated_at,updated_by)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (str(item.get('item_key','')),str(item.get('factory_key','')),str(item.get('factory_name','')),
                             str(item.get('workshop_key','')),str(item.get('workshop_name','')),str(item.get('equipment_name','')),
                             str(item.get('bundle_id','')),str(item.get('bundle_name','')),str(item.get('part_name','')),
                             str(item.get('part_code','')),str(item.get('unit','件')),item.get('quantity'),item.get('min_stock'),
                             str(item.get('location','')),str(item.get('updated_at') or now_iso()),str(item.get('updated_by',''))))
                    for entry in reversed(transactions):
                        if not isinstance(entry, dict):
                            continue
                        cols = ('item_key','factory_name','workshop_name','equipment_name','part_name','action','quantity',
                                'before_qty','after_qty','operator','batch','supplier','note','created_at')
                        conn.execute('''INSERT INTO inventory_transactions
                            (item_key,factory_name,workshop_name,equipment_name,part_name,action,quantity,before_qty,after_qty,
                             operator,batch,supplier,note,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            tuple(entry.get(k) for k in cols[:-1]) + (entry.get('created_at') or now_iso(),))
                    conn.commit()
                return self.send_json(200, {'ok': True, 'itemsImported': len(items), 'transactionsImported': len(transactions)})
            if path == '/api/transactions':
                with DB_LOCK, connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    result = apply_transaction(conn, data)
                    conn.commit()
                return self.send_json(200, {'ok': True, **result})
            if path == '/api/read-xlsx':
                filename = str(data.get('filename', 'upload.xlsx'))
                encoded = data.get('contentBase64', '')
                if not filename.lower().endswith('.xlsx'):
                    raise ValueError('文件扩展名不是 .xlsx')
                try:
                    file_bytes = base64.b64decode(encoded, validate=True)
                except Exception:
                    raise ValueError('文件内容编码无效。')
                rows = rows_from_xlsx(file_bytes)
                return self.send_json(200, {'ok': True, 'rows': rows})
            return self.send_error(404, 'Not found')
        except ValueError as e:
            return self.send_json(400, {'error': str(e)})
        except RuntimeError as e:
            return self.send_json(500, {'error': str(e)})
        except Exception as e:
            print('ERROR:', repr(e))
            return self.send_json(500, {'error': '服务器内部错误：' + str(e)})


def main():
    parser = argparse.ArgumentParser(description='帕鲁特金矿库存协同服务')
    parser.add_argument('--host', default=os.environ.get('HOST', '127.0.0.1'), help='服务器监听地址')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8765')))
    args = parser.parse_args()
    config_error = auth_configuration_error()
    if config_error:
        raise SystemExit('启动失败：' + config_error)
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not AUTH_ENABLED:
        raise SystemExit('拒绝启动：非本机监听必须启用 APP_PASSWORD 登录保护。')
    init_db()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print('库存协同服务已启动')
    print('服务监听端口：%s' % args.port)
    print('登录保护：%s' % ('已启用' if AUTH_ENABLED else '仅本机开发模式（未启用登录）'))
    print('SQLite库存库：%s' % DB_FILE)
    print('停止服务：Ctrl+C')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n服务已停止')
    finally:
        httpd.server_close()

if __name__ == '__main__':
    main()
