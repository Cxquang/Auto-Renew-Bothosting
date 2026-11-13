#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, random, json, requests, subprocess,ctypes
from urllib.parse import quote, unquote
from seleniumbase import SB

# 环境变量配置(可以直接私库在双引号里填写)
EMAIL         = os.environ.get("EMAIL") or ""           # 邮箱必填，登录和通知使用
PASSWORD      = os.environ.get("PASSWORD") or ""        # 主用密码登录方式, 失败时才使用,必须填写
MP_URL        = (os.environ.get("MP_URL") or "").strip()  # MoviePilot 域名/地址, 未写协议时默认 https://
MP_APIKEY     = os.environ.get("MP_APIKEY") or ""       # MoviePilot UrlNotify 插件的 apikey
MP_CHANNEL    = os.environ.get("MP_CHANNEL") or ""      # 可选, 指定通知渠道(Wechat/Feishu/Telegram 等), 不填使用插件默认
MP_MTYPE      = os.environ.get("MP_MTYPE") or ""        # 可选, 指定消息类型, 不填使用插件默认

if MP_URL and not MP_URL.startswith(("http://", "https://")):
    MP_URL = "https://" + MP_URL
MP_URL = MP_URL.rstrip("/")

BASE_URL = "https://bot-hosting.net"
BILLINGS_URL = f"{BASE_URL}/dash/billings"  # 站点新版账单页路径(旧版 /a/billings 会重定向到这里)

# ---------- 代理地址归一化 ----------
# 兼容几种常见写法:
#   http://user:pass@host:port / user:pass@host:port / host:port:user:pass / host:port / socks5://host:port
# SeleniumBase 的认证代理只认 user:pass@host:port(HTTP), Chrome 不支持 SOCKS 认证
def normalize_proxy(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return "", "", ""
    scheme = "http"
    rest = raw
    m = re.match(r"^(socks5h?|socks4a?|https?)://(.+)$", raw, re.I)
    if m:
        scheme = m.group(1).lower()
        rest = m.group(2)
        if scheme == "socks5h":
            scheme = "socks5"
    user = pwd = ""
    if "@" in rest:
        cred, hostport = rest.rsplit("@", 1)
        if ":" in cred:
            user, pwd = cred.split(":", 1)
        else:
            user = cred
    elif rest.count(":") == 3:  # b2proxy 导出的 host:port:user:pass
        host, port, user, pwd = rest.split(":", 3)
        hostport = f"{host}:{port}"
    else:
        hostport = rest
    user, pwd = unquote(user), unquote(pwd)
    if scheme.startswith("socks"):
        if user:
            print("⚠️ Chrome 不支持 SOCKS5 认证, 请改用代理商提供的 HTTP 端口")
        sb_proxy = f"{scheme}://{hostport}"
    elif user:
        sb_proxy = f"{user}:{pwd}@{hostport}"
    else:
        sb_proxy = hostport
    req_proxy = f"{scheme}://"
    if user:
        req_proxy += f"{quote(user, safe='')}:{quote(pwd, safe='')}@"
    req_proxy += hostport
    display = f"{scheme}://{hostport}" + (f" (用户: {user})" if user else "")
    return sb_proxy, req_proxy, display

if not (EMAIL and PASSWORD):
    print("ℹ️ 未配置 EMAIL 和 PASSWORD,脚本终止。")
    sys.exit(1)

# 发送通知(经 MoviePilot UrlNotify 插件转发到企业微信)
def send_moviepilot_message(message: str):
    if not MP_URL or not MP_APIKEY:
        print("⚠️ MoviePilot 通知未配置(MP_URL/MP_APIKEY)，跳过通知")
        return
    url = f"{MP_URL}/api/v1/plugin/UrlNotify/send"
    params = {"apikey": MP_APIKEY}
    if MP_CHANNEL:
        params["channel"] = MP_CHANNEL
    if MP_MTYPE:
        params["mtype"] = MP_MTYPE
    try:
        resp = requests.post(
            url,
            params=params,
            json={"title": "Bot-hosting 续期通知", "text": message},
            timeout=15,
        )
        resp.raise_for_status()
        print(f"✅ MoviePilot 通知已发送: {resp.text[:200]}")
    except Exception as e:
        print(f"❌ MoviePilot 通知发送失败: {e}")

# 通知格式
def format_notification(status: str, extra: str = "", error: str = "", expiry_date: str = "") -> str:
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****' 
    
    lines = [
        "🇫🇮 Bot-hosting 续期通知",
        "",
        f"{status}",
        f"👤 登录账户: {masked_email}",
    ]
    if expiry_date:
        lines.append(f"📅 到期时间: {expiry_date}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 登录时间: {now}")
    return "\n".join(lines)

# ---------- 基础工具 ----------
def js(sb, script, default=None, errors=None):
    try:
        result = sb.execute_script(script)
        return default if result is None else result
    except Exception as e:
        if errors is not None:
            errors.append(f"execute_script: {type(e).__name__}: {e}")
        try:
            result = sb.cdp.evaluate("(function(){ " + script + " })()")
            return default if result is None else result
        except Exception as e2:
            if errors is not None:
                errors.append(f"cdp.evaluate: {type(e2).__name__}: {e2}")
            return default

# Turnstile 定位失败时的现场诊断快照
def diag_turnstile_dom(sb, errors=None):
    snap = js(sb, """
        var out = [];
        var ifr = document.querySelectorAll('iframe');
        out.push('top-doc iframes: ' + ifr.length);
        for (var i = 0; i < ifr.length; i++) {
            var r = ifr[i].getBoundingClientRect();
            out.push('iframe w=' + Math.round(r.width) + ' h=' + Math.round(r.height) + ' src=' + (ifr[i].src || '').slice(0, 90));
        }
        var ts = document.querySelectorAll('[class*="turnstile"], [id*="turnstile"], input[name="cf-turnstile-response"], [data-sitekey]');
        out.push('turnstile-ish elements: ' + ts.length);
        for (var j = 0; j < ts.length; j++) {
            var rr = ts[j].getBoundingClientRect();
            var cls = (typeof ts[j].className === 'string') ? ts[j].className : (ts[j].getAttribute('class') || '');
            out.push(' - ' + ts[j].tagName + ' class=' + cls + ' w=' + Math.round(rr.width) + ' h=' + Math.round(rr.height));
        }
        var scripts = document.querySelectorAll('script[src*="challenges.cloudflare.com"]');
        out.push('turnstile script tags: ' + scripts.length);
        for (var k = 0; k < scripts.length; k++) {
            out.push(' - script src=' + String(scripts[k].src || '').slice(0, 120));
        }
        out.push('window.turnstile=' + (typeof window.turnstile) + ' readyState=' + document.readyState);
        return out.join('\\n');
    """, "")
    for line in str(snap).splitlines():
        print("   [diag] " + line)
    if errors:
        for err in errors:
            print("   [diag][exec-error] " + str(err)[:200])

# ---------- 续期全链路诊断日志 ----------
# 安装网络/控制台监听: fetch/XHR 请求与响应、console.error/warn、页面异常全部落 localStorage,
# 页面刷新后仍可读取, 用于定位"点击了但没生效"的真实原因
DIAG_INSTALL_JS = """
window.__diagNet = window.__diagNet || [];
window.__diagConsole = window.__diagConsole || [];
function __diagPush(key, storageKey, entry, max) {
    var arr = window[key] = window[key] || [];
    arr.push(entry);
    while (arr.length > max) arr.shift();
    try { localStorage.setItem(storageKey, JSON.stringify(arr)); } catch (e) {}
}
if (!window.__diagNet.length) {
    try {
        var prevNet = JSON.parse(localStorage.getItem('__diagNetLog') || '[]');
        if (prevNet && prevNet.length) window.__diagNet = prevNet;
    } catch (e) {}
}
if (!window.__diagConsole.length) {
    try {
        var prevCon = JSON.parse(localStorage.getItem('__diagConsoleLog') || '[]');
        if (prevCon && prevCon.length) window.__diagConsole = prevCon;
    } catch (e) {}
}
if (!window.__diagNetInstalled) {
    window.__diagNetInstalled = true;
    var origFetch = window.fetch;
    if (origFetch) {
        window.fetch = function () {
            var args = arguments, url = '', method = 'GET', reqBody = '', start = Date.now();
            try {
                var req = args[0];
                if (typeof req === 'string') { url = req; }
                else if (req && req.url) { url = req.url; method = req.method || 'GET'; }
                if (args[1] && args[1].method) method = args[1].method;
                if (args[1] && args[1].body) reqBody = String(args[1].body).slice(0, 500);
            } catch (e) {}
            return origFetch.apply(this, args).then(function (resp) {
                __diagPush('__diagNet', '__diagNetLog', {ts: Date.now(), t: 'fetch', method: method, url: String(url).slice(0, 300), status: resp.status, ms: Date.now() - start, req: reqBody}, 60);
                try {
                    resp.clone().text().then(function (body) {
                        __diagPush('__diagNet', '__diagNetLog', {ts: Date.now(), t: 'fetch-body', method: method, url: String(url).slice(0, 300), status: resp.status, body: String(body || '').slice(0, 1000)}, 60);
                    }).catch(function () {});
                } catch (e) {}
                return resp;
            }).catch(function (err) {
                __diagPush('__diagNet', '__diagNetLog', {ts: Date.now(), t: 'fetch-error', method: method, url: String(url).slice(0, 300), error: String(err).slice(0, 300)}, 60);
                throw err;
            });
        };
    }
    var origOpen = XMLHttpRequest.prototype.open;
    var origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
        this.__diagInfo = {method: method, url: String(url)};
        return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function (body) {
        var xhr = this, start = Date.now();
        try {
            xhr.addEventListener('loadend', function () {
                var info = xhr.__diagInfo || {};
                __diagPush('__diagNet', '__diagNetLog', {ts: Date.now(), t: 'xhr', method: info.method, url: String(info.url || '').slice(0, 300), status: xhr.status, ms: Date.now() - start, req: String(body || '').slice(0, 500), body: String(xhr.responseText || '').slice(0, 1000)}, 60);
            });
        } catch (e) {}
        return origSend.apply(this, arguments);
    };
}
if (!window.__diagConsoleInstalled) {
    window.__diagConsoleInstalled = true;
    ['error', 'warn'].forEach(function (level) {
        var orig = console[level];
        console[level] = function () {
            try {
                var parts = Array.prototype.map.call(arguments, function (a) {
                    try { return (typeof a === 'string') ? a : JSON.stringify(a); } catch (e) { return String(a); }
                });
                __diagPush('__diagConsole', '__diagConsoleLog', {ts: Date.now(), level: level, text: parts.join(' ').slice(0, 600)}, 40);
            } catch (e) {}
            return orig.apply(console, arguments);
        };
    });
    window.addEventListener('error', function (e) {
        __diagPush('__diagConsole', '__diagConsoleLog', {ts: Date.now(), level: 'pageerror', text: String((e && e.message) || e).slice(0, 400)}, 40);
    });
    window.addEventListener('unhandledrejection', function (e) {
        var r = e.reason;
        __diagPush('__diagConsole', '__diagConsoleLog', {ts: Date.now(), level: 'unhandledrejection', text: String((r && r.message) || r).slice(0, 400)}, 40);
    });
}
return 'net=' + window.__diagNet.length + ', console=' + window.__diagConsole.length;
"""

DIAG_SNAPSHOT_JS = """
var out = [];
var clean = function (s) { return String(s || '').replace(/\\s+/g, ' ').trim(); };
var rawBody = document.body ? (document.body.innerText || '') : '';
out.push('url=' + location.href);
out.push('title=' + clean(document.title) + ' readyState=' + document.readyState);
if (/just a moment|checking your browser|verify you are human/i.test(rawBody)) out.push('WARN=疑似 Cloudflare 挑战页');
var dialogs = document.querySelectorAll('[role=dialog]');
out.push('dialogs=' + dialogs.length);
for (var i = 0; i < dialogs.length; i++) {
    var d = dialogs[i], dr = d.getBoundingClientRect();
    out.push('dialog[' + i + '] visible=' + (dr.width > 0 && dr.height > 0) + ' size=' + Math.round(dr.width) + 'x' + Math.round(dr.height) + ' text="' + clean(d.innerText || d.textContent).slice(0, 400) + '"');
    var btns = d.querySelectorAll('button, input[type=submit], a.btn');
    out.push('dialog[' + i + '] buttons=' + btns.length);
    for (var j = 0; j < btns.length; j++) {
        var b = btns[j], br = b.getBoundingClientRect();
        out.push('  btn[' + j + '] tag=' + b.tagName.toLowerCase() + ' text="' + clean(b.innerText || b.value) + '" class="' + clean(b.getAttribute('class')).slice(0, 120) + '" disabled=' + (b.disabled === true) + ' aria-disabled=' + b.getAttribute('aria-disabled') + ' visible=' + (br.width > 0 && br.height > 0) + ' rect=' + Math.round(br.x) + ',' + Math.round(br.y) + ',' + Math.round(br.width) + 'x' + Math.round(br.height));
    }
}
var toasts = document.querySelectorAll('[role=alert], [class*=toast], [class*=Toast], [class*=alert], [class*=Alert], [class*=error], [class*=Error], [class*=notification], [class*=Notification]');
out.push('toast_like=' + toasts.length);
for (var k = 0; k < toasts.length && k < 12; k++) {
    var t = toasts[k], tr = t.getBoundingClientRect();
    if (tr.width > 0 && tr.height > 0) out.push('  toast[' + k + '] class="' + clean(t.getAttribute('class')).slice(0, 100) + '" text="' + clean(t.innerText || t.textContent).slice(0, 250) + '"');
}
var kw = /error|fail|invalid|unable|try again|try later|limit|cooldown|already|too many|slow down|captcha|verify|renew|success/i;
var lines = rawBody.split('\\n'), hits = [];
for (var m = 0; m < lines.length && hits.length < 15; m++) {
    var ln = clean(lines[m]);
    if (ln && kw.test(ln) && ln.length < 200) hits.push(ln);
}
out.push('keyword_lines=' + hits.length);
for (var n = 0; n < hits.length; n++) out.push('  kw: ' + hits[n]);
var pageBtns = document.querySelectorAll('button, a.btn');
var shownBtn = 0;
out.push('page_visible_buttons:');
for (var p = 0; p < pageBtns.length && shownBtn < 25; p++) {
    var pb = pageBtns[p], pr = pb.getBoundingClientRect();
    if (pr.width > 0 && pr.height > 0) {
        shownBtn += 1;
        out.push('  pagebtn<' + pb.tagName.toLowerCase() + '> text="' + clean(pb.innerText || pb.value) + '" class="' + clean(pb.getAttribute('class')).slice(0, 100) + '" disabled=' + (pb.disabled === true) + ' aria-disabled=' + pb.getAttribute('aria-disabled'));
    }
}
var tokenLen = 0;
try {
    if (window.turnstile && window.turnstile.getResponse) { var v = window.turnstile.getResponse(); tokenLen = v ? v.length : 0; }
} catch (e) {}
if (!tokenLen) { var el = document.querySelector('input[name="cf-turnstile-response"]'); tokenLen = el && el.value ? el.value.length : 0; }
out.push('turnstile_token_len=' + tokenLen + ' widgets=' + document.querySelectorAll('.cf-turnstile, [data-sitekey]').length + ' iframes=' + document.querySelectorAll('iframe').length);
return out.join('\\n');
"""

CONFIRM_BTN_INFO_JS = """
var d = document.querySelector('div[role=dialog]');
if (!d) return 'NO_DIALOG_FOUND';
var clean = function (s) { return String(s || '').replace(/\\s+/g, ' ').trim(); };
var out = ['dialog_text="' + clean(d.innerText || d.textContent).slice(0, 300) + '"'];
var btns = d.querySelectorAll('button.btn-primary');
out.push('button.btn-primary count=' + btns.length);
for (var i = 0; i < btns.length; i++) {
    var b = btns[i], r = b.getBoundingClientRect();
    out.push('btn[' + i + '] text="' + clean(b.innerText) + '" disabled=' + (b.disabled === true) + ' aria-disabled=' + b.getAttribute('aria-disabled') + ' visible=' + (r.width > 0 && r.height > 0) + ' outerHTML=' + String(b.outerHTML || '').slice(0, 300));
}
return out.join('\\n');
"""

def diag_install(sb):
    """挂载网络/控制台监听(每次页面刷新后需重新挂载)"""
    info = js(sb, DIAG_INSTALL_JS, "")
    print(f"🧪 诊断监听已挂载: {info}")

def diag_marker(sb, name: str):
    """在网络日志里打时间标记, 便于区分各阶段请求"""
    js(sb, "window.__diagNet = window.__diagNet || [];"
           "window.__diagNet.push({ts: Date.now(), t: 'marker', url: 'MARKER:" + str(name) + "'});"
           "try { localStorage.setItem('__diagNetLog', JSON.stringify(window.__diagNet)); } catch (e) {}"
           "return 'ok';", "")

def diag_snapshot(sb, tag: str = "诊断"):
    """打印弹窗/按钮/Toast/关键词/Turnstile 现场快照"""
    snap = js(sb, DIAG_SNAPSHOT_JS, "")
    print(f"🔍 [{tag}] 页面快照:")
    for line in str(snap).splitlines():
        print(f"   [{tag}] {line}")

def _diag_read(sb, mem_expr: str, storage_key: str) -> list:
    raw = js(sb, "return JSON.stringify(" + mem_expr + " || []);", "")
    if not raw or raw == "[]":
        raw = js(sb, "try { return localStorage.getItem('" + storage_key + "') || '[]'; } catch (e) { return '[]'; }", "")
    try:
        items = json.loads(raw)
        return items if isinstance(items, list) else []
    except Exception:
        return []

def diag_network(sb, tag: str = "网络"):
    """打印网络请求/响应(含续期 POST 的请求体与响应体)"""
    items = _diag_read(sb, "window.__diagNet", "__diagNetLog")
    print(f"🌐 [{tag}] 网络记录 ({len(items)} 条):")
    if not items:
        print(f"   [{tag}] 无记录")
        return
    for it in items[-45:]:
        ts = time.strftime("%H:%M:%S", time.gmtime(it.get("ts", 0) / 1000 + 8 * 3600)) if it.get("ts") else "-"
        line = f"{ts} {it.get('t')} {it.get('method', '')} {str(it.get('url', ''))[:180]} status={it.get('status', '-')}"
        if it.get("ms") is not None:
            line += f" {it.get('ms')}ms"
        if it.get("req"):
            line += f" req={str(it.get('req'))[:300]}"
        if it.get("body"):
            line += f" body={str(it.get('body'))[:600]}"
        if it.get("error"):
            line += f" error={str(it.get('error'))[:200]}"
        print(f"   [{tag}] {line}")

def diag_console(sb, tag: str = "控制台"):
    """打印 console.error/warn 与页面未捕获异常"""
    items = _diag_read(sb, "window.__diagConsole", "__diagConsoleLog")
    print(f"🖥️ [{tag}] 控制台记录 ({len(items)} 条):")
    if not items:
        print(f"   [{tag}] 无记录")
        return
    for it in items[-30:]:
        print(f"   [{tag}] {it.get('level')}: {str(it.get('text', ''))[:400]}")

# 从诊断日志里取最近一条匹配的请求记录(用于确认登录请求是否真的发出)
def last_api_call(sb, path_kw: str, since_ts: int = 0):
    items = _diag_read(sb, "window.__diagNet", "__diagNetLog")
    found = None
    for it in items:
        if not isinstance(it, dict):
            continue
        if path_kw not in str(it.get("url", "")):
            continue
        try:
            if int(it.get("ts") or 0) < since_ts:
                continue
        except Exception:
            pass
        found = it
    return found

# 在页面里真实加载 Turnstile api.js, 判断浏览器(经代理)能否访问 challenges.cloudflare.com
TS_PROBE_JS = """
if (typeof window.turnstile !== 'undefined') return 'ok';
window.__tsProbe = window.__tsProbe || 'pending';
if (!document.getElementById('__tsProbe')) {
    var s = document.createElement('script');
    s.id = '__tsProbe';
    s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&_probe=' + Date.now();
    s.onload = function () { window.__tsProbe = 'ok'; };
    s.onerror = function () { window.__tsProbe = 'error'; };
    document.head.appendChild(s);
}
return window.__tsProbe;
"""

def probe_turnstile_script(sb, timeout=15) -> str:
    deadline = time.time() + timeout
    state = "pending"
    while time.time() < deadline:
        state = js(sb, TS_PROBE_JS, "pending")
        if state in ("ok", "error"):
            break
        time.sleep(1)
    state = str(state or "pending")
    if state == "ok":
        print("✅ 浏览器内 Turnstile 脚本可加载(代理线路可访问 challenges.cloudflare.com)")
    elif state == "error":
        print("❌ 浏览器内 Turnstile 脚本加载失败: 该代理线路无法访问 challenges.cloudflare.com")
    else:
        print(f"⚠️ 浏览器内 Turnstile 脚本 {timeout} 秒未加载完成(线路过慢或被拦截)")
    return state

#  页面注入脚本
# Turnstile token（官方 JS 接口 或 表单隐藏域），未通过时为空串
TURNSTILE_TOKEN_JS = """
try { if (window.turnstile && window.turnstile.getResponse) {
        var t = window.turnstile.getResponse(); if (t) return t; } } catch (e) {}
var el = document.querySelector('input[name="cf-turnstile-response"]');
return el ? (el.value || '') : '';
"""

TURNSTILE_EXISTS_JS = """
return document.querySelector(
    'input[name="cf-turnstile-response"], iframe[src*="challenges.cloudflare.com"], .cf-turnstile'
) !== null;
"""
TS_COORDS_JS = """
function findWidget(root) {
    var els = root.querySelectorAll(
        'iframe[src*="challenges.cloudflare.com"], .cf-turnstile, [id*="cf-chl"]');
    for (var i = 0; i < els.length; i++) {
        var r = els[i].getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return els[i];
    }
    var all = root.querySelectorAll('*');
    for (var j = 0; j < all.length; j++) {
        if (all[j].shadowRoot) {
            var f = findWidget(all[j].shadowRoot);
            if (f) return f;
        }
    }
    return null;
}
function boxFromTokenInput() {
    var el = document.querySelector('input[name="cf-turnstile-response"]');
    var cur = el ? el.parentElement : null;
    for (var k = 0; k < 6 && cur; k++) {
        var r = cur.getBoundingClientRect();
        if (r.width > 100 && r.width < 600 && r.height > 30 && r.height < 140) return cur;
        cur = cur.parentElement;
    }
    return null;
}
var hit = findWidget(document) || boxFromTokenInput();
if (!hit) return null;
var hr = hit.getBoundingClientRect();
return {cx: Math.round(hr.x + 30), cy: Math.round(hr.y + hr.height / 2)};
"""

# Turnstile 脚本异步渲染，轮询等待组件出现
def wait_turnstile_present(sb, timeout=12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if js(sb, TURNSTILE_EXISTS_JS, False):
            return True
        time.sleep(1)
    return False

def _activate_window():
    if os.name == "nt":
        return

    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls], capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]], timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"], timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _windows_mouse_click(x: int, y: int):
    try:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.08)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
    except Exception:
        pass

def _xdotool_click(x: int, y: int):
    if os.name == "nt":
        _windows_mouse_click(x, y)
        return

    _activate_window()
    try:
        subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)], timeout=3, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
    except Exception:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")

def turnstile_token(sb) -> str:
    token = js(sb, TURNSTILE_TOKEN_JS, "")
    return token if isinstance(token, str) else ""

def _click_turnstile(sb, locate_timeout=10) -> bool:
    coords = None
    errors = []
    deadline = time.time() + locate_timeout
    while time.time() < deadline:
        coords = js(sb, TS_COORDS_JS, None, errors)
        if coords:
            break
        time.sleep(1)
    if not coords:
        print("⚠️ 无法定位 Turnstile 坐标, 打印现场诊断:")
        diag_turnstile_dom(sb, errors)
        return False

    try:
        wi = js(sb, "return '' + (window.screenX || 0) + '|' + (window.screenY || 0) + '|' +"
                    " window.outerHeight + '|' + window.innerHeight;", "")
        parts = str(wi).split("|")
        sx, sy = int(parts[0] or 0), int(parts[1] or 0)
        bar = int(parts[2] or 800) - int(parts[3] or 768)
        ax = int(coords["cx"]) + sx
        ay = int(coords["cy"]) + sy + bar
        print(f"🖱️ 底层点击 Turnstile ({ax}, {ay})")
        _xdotool_click(ax, ay)
        time.sleep(3)
        return True
    except Exception as e:
        print(f"⚠️ 底层点击失败: {e}")
        return False

def click_turnstile(sb) -> bool:
    if _click_turnstile(sb):
        return True
    coords = js(sb, TS_COORDS_JS)
    if coords:
        try:
            if hasattr(sb, "cdp") and hasattr(sb.cdp, "gui_click_x_y"):
                print(f"🖱️ 坐标点击 Turnstile ({coords['cx']},{coords['cy']})")
                sb.cdp.gui_click_x_y(int(coords["cx"]), int(coords["cy"]))
                return True
        except Exception as e:
            print(f"⚠️ 坐标点击失败: {e}")
    try:
        if hasattr(sb, "cdp") and hasattr(sb.cdp, "click_captcha"):
            print("🖱️ 兜底调用cdp点击 ...")
            sb.cdp.click_captcha()
            return True
        print("⚠️ 当前 SeleniumBase 版本无 sb.cdp.click_captcha")
    except Exception as e:
        print(f"⚠️ click_captcha 出错: {e}")
    return False

def solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8) -> bool:
    if turnstile_token(sb):
        return True

    deadline = time.time() + timeout
    next_click = time.time() + first_wait
    clicks = 0
    while time.time() < deadline:
        if turnstile_token(sb):
            print(f"✅ {tag}已通过(已获取 token)")
            return True
        if time.time() >= next_click:
            clicks += 1
            print(f"🖱️ 第 {clicks} 次点击 Turnstile 验证框...")
            click_turnstile(sb)
            next_click = time.time() + 10 + random.uniform(0, 3)
        time.sleep(2)
    ok = bool(turnstile_token(sb))
    print(f"{'✅' if ok else '❌'} {tag}{'已通过' if ok else '未通过'}")
    return ok

def handle_turnstile(sb) -> bool:
    print("🛡️ 处理 Turnstile 验证...")
    return solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8)

# 登录页 Turnstile: 等待渲染并处理; 长时间未出现时判断代理线路能否加载 Cloudflare 脚本
def handle_login_turnstile(sb, wait_timeout=40) -> bool:
    # Turnstile 由 api.js 异步渲染, 走住宅代理时首次加载明显更慢
    if wait_turnstile_present(sb, timeout=wait_timeout):
        return handle_turnstile(sb)
    print(f"ℹ️ {wait_timeout} 秒内未检测到 Turnstile, 打印现场诊断")
    diag_turnstile_dom(sb)
    state = probe_turnstile_script(sb)
    diag_console(sb, "登录页控制台")
    diag_network(sb, "登录页网络")
    if state == "ok":
        print("⏳ Turnstile 脚本可加载, 再等待组件渲染 20 秒...")
        if wait_turnstile_present(sb, timeout=20):
            return handle_turnstile(sb)
    print("⚠️ 登录页 Turnstile 不可用, 前端会拦截登录请求")
    return False

# 打开登录页 -> Continue with email -> 填写邮箱密码(供失败重试复用)
def open_login_form(sb):
    print(f"🌐 访问 {BASE_URL}/login")
    sb.open(f"{BASE_URL}/login")
    sb.wait_for_ready_state_complete()
    sb.sleep(3)
    diag_install(sb)  # 登录请求也纳入诊断(确认 /api/auth/email 是否真的发出)
    sb.click('button[type="submit"]')  # 点击 Continue with email 按钮
    try:
        sb.wait_for_element('input[type="email"]', timeout=25)
    except Exception:
        print("⚠️ 25 秒内未出现邮箱输入框, 继续尝试填写")
    sb.sleep(2)
    print("📧 填写邮箱...")
    sb.input('input[type="email"]', EMAIL)
    sb.sleep(1)
    print("🔑 填写密码...")
    sb.input('input[type="password"]', PASSWORD)
    sb.sleep(3)
    
# 获取当前出口ip
def get_current_ip(proxy_server: str = "") -> str:
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    response.raise_for_status()
    return response.text.strip()

# 时间格式化
def format_countdown(countdown_str: str) -> str:
    try:
        h, m, _ = countdown_str.split(':')
        h = int(h)
        m = int(m)
        if h > 0:
            return f"{h}h{m}min"
        else:
            return f"{m}min"
    except:
        return countdown_str

# 归一化日期为 YYYY/MM/DD
def normalize_date(date_str: str) -> str:
    parts = date_str.split('/')
    if len(parts) == 3:
        if len(parts[2]) == 4:   # MM/DD/YYYY
            return f"{parts[2]}/{int(parts[0]):02d}/{int(parts[1]):02d}"
        if len(parts[0]) == 4:   # YYYY/MM/DD
            return f"{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}"
    return date_str

DATE_RE = r"(\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}/\d{4})"

# 新版账单页使用英文月份日期(如 "14 Nov 2026"), 需要单独解析
MONTH_MAP = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

def parse_month_date(text: str):
    if not text:
        return None
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b", text)  # 14 Nov 2026
    if m:
        mon = MONTH_MAP.get(m.group(2).lower()[:3])
        if mon:
            return f"{m.group(3)}/{mon:02d}/{int(m.group(1)):02d}"
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", text)  # Nov 14, 2026
    if m:
        mon = MONTH_MAP.get(m.group(1).lower()[:3])
        if mon:
            return f"{m.group(3)}/{mon:02d}/{int(m.group(2)):02d}"
    return None

# 获取过期日期
def extract_expiry_date(page_source: str) -> str:
    month_date = parse_month_date(page_source)
    if month_date:
        return month_date
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*" + DATE_RE,   # Expires 10/24/2026. Renew manually to extend it for 4 days.
        DATE_RE,                                # 兜底: 账单页纯文本中通常只有到期日期
    ]
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            return normalize_date(match.group(1))
    return None

# 优先从 DOM 的 st-meta 元素提取到期日期 (不依赖页面语言)
def get_expiry_from_dom(sb) -> str:
    for selector in ['span.st-meta', '.st-meta', '[class*="st-meta"]']:
        try:
            if sb.is_element_present(selector):
                text = sb.get_text(selector)
                month_date = parse_month_date(text)
                if month_date:
                    return month_date
                match = re.search(DATE_RE, text)
                if match:
                    return normalize_date(match.group(1))
        except Exception:
            pass
    return None

# ---------- 真人化点击 / 服务端错误提取 ----------
OUTER_RENEW_FINDER_JS = """(function(){var bs=document.querySelectorAll('button,a');
for(var i=0;i<bs.length;i++){var t=(bs[i].innerText||'').trim();
if(/^renew( free plan)?$/i.test(t)){return bs[i];}}return null;})()"""

MODAL_CONFIRM_FINDER_JS = """document.querySelector('div[role="dialog"] button.btn-primary')"""

# 用 OS 级鼠标事件点击元素中心: 先随机停顿, 再先移到附近位置后移到目标, 减少"瞬移点击"特征
def human_click(sb, finder_js: str, tag: str = "") -> bool:
    try:
        coords = js(sb, "return (function(){var el=" + finder_js + ";if(!el)return null;"
                        "el.scrollIntoView({block:'center'});var r=el.getBoundingClientRect();"
                        "if(r.width<2||r.height<2)return null;"
                        "return {cx:Math.round(r.x+r.width/2),cy:Math.round(r.y+r.height/2)};})();")
        if not coords:
            print(f"⚠️ 未定位到{tag}坐标")
            return False
        time.sleep(random.uniform(0.5, 1.5))
        wi = js(sb, "return '' + (window.screenX || 0) + '|' + (window.screenY || 0) + '|' +"
                    " window.outerHeight + '|' + window.innerHeight;", "")
        parts = str(wi).split("|")
        sx, sy = int(parts[0] or 0), int(parts[1] or 0)
        bar = int(parts[2] or 800) - int(parts[3] or 768)
        ax, ay = int(coords["cx"]) + sx, int(coords["cy"]) + sy + bar
        if os.name != "nt":
            _activate_window()
            subprocess.run(["xdotool", "mousemove", "--sync",
                            str(ax + random.randint(-40, 40)),
                            str(ay + random.randint(-25, 25))],
                           timeout=3, stderr=subprocess.DEVNULL)
            time.sleep(random.uniform(0.15, 0.4))
        print(f"🖱️ 真人点击{tag} ({ax}, {ay})")
        _xdotool_click(ax, ay)
        time.sleep(random.uniform(0.3, 0.8))
        return True
    except Exception as e:
        print(f"⚠️ 真人点击{tag}失败: {e}")
        return False

# 从诊断日志里取最近一次 /api/billing/renew-free 的失败响应(含服务端错误码)
def last_renew_api_error(sb, since_ts: int = 0):
    entries = js(sb, "try { return JSON.parse(localStorage.getItem('__diagNetLog') || '[]'); }"
                     " catch (e) { return []; }", [])
    if not isinstance(entries, list):
        return None
    found = None
    for e in entries:
        if not isinstance(e, dict):
            continue
        if "/api/billing/renew-free" not in str(e.get("url", "")):
            continue
        try:
            if int(e.get("ts") or 0) < since_ts:
                continue
        except Exception:
            pass
        status = int(e.get("status") or 0)
        if status >= 400:
            found = e  # 保留最后一条(通常带响应 body)
    return found

# 点击确认后轮询账单页，确认续期是否真的生效（只返回成功或失败，不再输出“可能未成功”）
# 返回 (是否成功, 最新到期日, 冷却倒计时, 说明)
def confirm_renewal_result(sb, current_expiry: str, timeout: int = 60) -> tuple:
    deadline = time.time() + timeout
    start = time.time()
    start_ms = int(start * 1000)
    last_expiry = current_expiry
    round_no = 0
    while True:
        round_no += 1
        sb.sleep(3)
        # 第 1 轮先看点击后的当前页面, 之后带时间戳重新打开账单页(避开前端缓存)
        if round_no > 1:
            try:
                sb.open(f"{BILLINGS_URL}?_r={int(time.time())}")
                sb.wait_for_ready_state_complete()
                sb.sleep(2)
            except Exception as e:
                print(f"⚠️ 刷新账单页失败: {e}")
        # 页面刷新后监听会丢失, 每轮重新挂载
        diag_install(sb)
        try:
            text = sb.get_page_text() + "\n" + sb.get_page_source()
        except Exception:
            text = sb.get_page_source()
        try:
            current_url = sb.get_current_url()
        except Exception:
            current_url = "-"
        dialog_count = js(sb, "return document.querySelectorAll('[role=dialog]').length;", 0)
        latest = get_expiry_from_dom(sb) or extract_expiry_date(text)
        if latest:
            last_expiry = latest
        match = re.search(r"(?:Renewal cooldown|Renew in)\s+(\d{1,2}:\d{2}:\d{2})", text)
        print(f"🔁 [轮询 {round_no}] 已用 {int(time.time() - start)}s url={current_url} "
              f"到期={latest or '未解析'} 倒计时={match.group(1) if match else '无'} 弹窗数={dialog_count}")
        if match:
            diag_snapshot(sb, f"轮询{round_no}成功现场")
            diag_network(sb, f"轮询{round_no}网络")
            return True, last_expiry, match.group(1), "账单页已出现下次可续期倒计时"
        if current_expiry and latest and latest != current_expiry:
            diag_snapshot(sb, f"轮询{round_no}成功现场")
            diag_network(sb, f"轮询{round_no}网络")
            return True, last_expiry, "", f"到期时间已更新（{current_expiry} → {latest}）"
        if time.time() >= deadline:
            diag_snapshot(sb, "最终失败现场")
            diag_network(sb, "最终失败网络")
            diag_console(sb, "最终失败控制台")
            reason = f"{timeout} 秒内账单页未出现续期倒计时，到期时间也未变化"
            api_err = last_renew_api_error(sb, start_ms)
            if api_err:
                body = str(api_err.get("body") or "").strip()
                reason += f"; 服务端 HTTP {api_err.get('status')}: {body[:200]}"
            return False, last_expiry, "", reason
        sb.sleep(4)

# 主流程
def main():
    print("#" * 25)
    print("   Bot-hosting 自动续期")
    print("#" * 25)

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    PROXY_RAW = os.environ.get("PROXY_SERVER", "").strip()
    if IS_PROXY and not PROXY_RAW:
        PROXY_RAW = "socks5://127.0.0.1:1080"  # 未显式配置时使用本地 sing-box
    SB_PROXY, PROXY_URL, PROXY_DISPLAY = normalize_proxy(PROXY_RAW)
    IS_PROXY = IS_PROXY and bool(SB_PROXY)
    HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true" 

    sb_kwargs = {"uc": True, "headless": HEADLESS, "locale_code": "en-US"}

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_DISPLAY}")
        sb_kwargs["proxy"] = SB_PROXY
    else:
        print("🍭 未使用代理，直连访问")

    with SB(**sb_kwargs) as sb:
        try:
            ip = get_current_ip(PROXY_URL if IS_PROXY else "")
            print(f"📍 当前出口IP: {ip}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        login_ok = False
        login_fail_reason = "Password 登录失败"

        # 账号密码登录（密码不过期，唯一登录方式；整段兜异常，失败只标记 login_ok=False 走统一失败通知）
        if EMAIL and PASSWORD:
            print("🚀 启动浏览器...")
            try:
                open_login_form(sb)
                if not handle_login_turnstile(sb):
                    print("🔄 重新加载登录页再试一次(排除首次加载慢/脚本加载失败)...")
                    open_login_form(sb)
                    if not handle_login_turnstile(sb, wait_timeout=25):
                        print("❌ 登录界面的 Turnstile 未就绪，尝试兜底点击...")
                        try:
                            sb.cdp.click_captcha()  # 再次尝试点击 Turnstile 验证
                            sb.sleep(4)
                        except Exception as e:
                            print(f"⚠️ 兜底点击失败: {e}")

                diag_marker(sb, "before_login_submit")
                submit_ts = int(time.time() * 1000)
                sb.sleep(1)
                sb.click('button[type="submit"]') # 点击登录按钮
                print("🖱️ 提交登录按钮")
                # 等待跳转或登录请求落地(最多 20 秒), 不再固定 sleep
                for _ in range(20):
                    sb.sleep(1)
                    try:
                        if "/login" not in sb.get_current_url():
                            break
                    except Exception:
                        pass
                    if last_api_call(sb, "/api/auth/email", submit_ts):
                        sb.sleep(3)  # 登录请求已发出, 留出跳转/报错渲染时间
                        break
                sb.wait_for_ready_state_complete()

                # 登录失败时页面上的报错文案(服务端 error 映射)
                page_err = js(sb, """
                    var e = document.querySelector('[role=alert], .alert, .error, [class*=error], [class*=Error]');
                    return e ? String(e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200) : '';
                """, "")
                if page_err:
                    print(f"🧾 [login] 页面提示: {page_err}")

                sb.open(BILLINGS_URL)
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                current_url = sb.get_current_url()
                current_title = sb.get_title()
                print(f"📝 当前URL: {current_url}, Title: {current_title}")

                # 兼容新旧路径(/dash/billings、/a/billings), 只要落在账单页且不在登录页即可
                if "billings" in current_url and "/login" not in current_url and "error=" not in current_url:
                    login_ok = True
                    print("✅ Password 登录成功,当前已到达账单页")
                else:
                    print(f"❌ Password 登录后仍未到达账单页，当前URL: {current_url}")
                    login_call = last_api_call(sb, "/api/auth/email", submit_ts)
                    if login_call:
                        status = login_call.get("status")
                        body = str(login_call.get("body") or "").strip()
                        print(f"   [login] POST /api/auth/email status={status} body={body[:300]}")
                        login_fail_reason = f"Password 登录失败(服务端返回 HTTP {status})"
                    else:
                        print("   [login] 未捕获到 /api/auth/email 请求: 前端因缺少 Turnstile token 中止提交")
                        login_fail_reason = "Password 登录失败(Turnstile 未加载, 登录请求未发出)"
                    diag_console(sb, "登录失败控制台")
                    diag_network(sb, "登录失败网络")
            except Exception as e:
                    print(f"❌ Password 登录过程异常: {type(e).__name__}: {str(e)[:150]}")
                    login_fail_reason = f"Password 登录异常: {type(e).__name__}"

        if not login_ok:
            send_moviepilot_message(format_notification("❌ 登录失败", error=login_fail_reason))
            return

        # 挂载网络/控制台诊断监听(续期全链路)
        diag_install(sb)

        # 提取当前到期日期
        sb.sleep(3)
        try:
            page_source = sb.get_page_text() + "\n" + sb.get_page_source()
        except Exception:
            page_source = sb.get_page_source()
        current_expiry = get_expiry_from_dom(sb) or extract_expiry_date(page_source)
        if current_expiry:
            print(f"📅 当前到期日期: {current_expiry}")
        else:
            print("⚠️ 未能提取当前到期日期")

        # 寻找外部续期按钮
        outer_renew_selector = None
        countdown_text = None
        possible_selectors = [
            'button:contains("Renew")',
            'button:contains("Renew free plan")',
            'a:contains("Renew")',
            '[class*="renew"]',
            '[class*="Renew"]',
        ]

        for selector in possible_selectors:
            try:
                if sb.is_element_visible(selector):
                    button_text = sb.get_text(selector)
                    # "Renew in HH:MM:SS" / "Renewal cooldown HH:MM:SS" 都表示冷却中, 时间为距可续期的剩余时长
                    match = re.search(r"(?:Renew in|Renewal cooldown)\s+(\d{1,2}:\d{2}:\d{2})", button_text, re.I)
                    if match:
                        countdown_text = match.group(1)
                        print(f"⏳ 续期冷却中, 未到续期时间: '{button_text}'")
                        break
                    elif "Renew" in button_text:
                        outer_renew_selector = selector
                        print(f"✅ 续期按钮可用: '{button_text}'")
                        break
            except Exception as e:
                pass

        # 点击外部续期按钮等待弹窗
        if outer_renew_selector:
            print(f"🔄 点击外部续期按钮，等待验证窗口... (selector={outer_renew_selector})")
            diag_marker(sb, "before_outer_renew_click")
            try:
                sb.sleep(random.uniform(1.0, 2.5))
                if not human_click(sb, OUTER_RENEW_FINDER_JS, "外部续期按钮"):
                    print("⚠️ 坐标点击未成功, 回退 WebDriver 点击")
                    sb.click(outer_renew_selector)
                sb.sleep(15)  # 等待模态框加载，可能因网络因素加载慢
                if js(sb, "return document.querySelectorAll('[role=dialog]').length;", 0) == 0:
                    print("⚠️ 15 秒内未出现弹窗, 回退 WebDriver 再点一次")
                    sb.click(outer_renew_selector)
                    sb.sleep(10)
            except Exception as e:
                print(f"❌ 点击外部按钮失败: {e}")
                diag_snapshot(sb, "点击外部按钮失败现场")
                diag_network(sb, "点击外部按钮失败网络")
                send_moviepilot_message(format_notification("❌ 续期失败", error="点击外部续期按钮出错"))
                return

            # 弹窗打开后的现场诊断: 是否真的有弹窗/按钮是否 disabled/文案是什么
            diag_snapshot(sb, "弹窗打开后")
            diag_network(sb, "弹窗打开后网络")

            # 处理弹窗中的 Turnstile
            turnstile_passed = True  # 未检测到 Turnstile 时视为无需验证
            if wait_turnstile_present(sb):
                turnstile_passed = handle_turnstile(sb)
                if not turnstile_passed:
                    print("❌ Turnstile 验证失败，尝试兜底点击...")
                    try:
                        sb.cdp.click_captcha()  # 再次尝试点击 Turnstile 验证
                        sb.sleep(4)
                        turnstile_passed = bool(turnstile_token(sb))
                    except Exception as e:
                        print(f"⚠️ 兜底点击失败: {e}")
                    # sb.save_screenshot("turnstile_failed.png")
            else:
                print("ℹ️ 未检测到 Turnstile")

            if not turnstile_passed:
                print("❌ Turnstile 验证最终未通过，脚本退出")
                diag_snapshot(sb, "Turnstile失败现场")
                diag_console(sb, "Turnstile失败控制台")
                send_moviepilot_message(format_notification("❌ 续期失败", error="Turnstile 验证未通过"))
                return

            # 点击续期按钮
            print("⏳ 等待续期按钮可用并点击...")
            time.sleep(5) 

            # 点击前诊断: 弹窗文本/所有 primary 按钮的文案与 disabled 状态
            diag_marker(sb, "before_confirm_click")
            diag_snapshot(sb, "点击确认前")
            print("🔎 弹窗确认按钮诊断:")
            btn_info = js(sb, CONFIRM_BTN_INFO_JS, "")
            for line in str(btn_info).splitlines():
                print(f"   [confirm] {line}")

            modal_button_clicked = False
            click_ts = int(time.time() * 1000)
            if human_click(sb, MODAL_CONFIRM_FINDER_JS, "弹窗确认按钮"):
                modal_button_clicked = True
                print("✅ 已点击续期按钮")
            else:
                try:
                    sb.click('div[role="dialog"] button.btn-primary', timeout=8)
                    modal_button_clicked = True
                    print("✅ 已点击续期按钮(WebDriver 回退)")
                except Exception as e:
                    print(f"续期按钮点击失败: {e}")

            if not modal_button_clicked:
                print("❌ 未点击到弹窗确认按钮，判定续期失败")
                diag_snapshot(sb, "未点到确认按钮现场")
                diag_network(sb, "未点到确认按钮网络")
                send_moviepilot_message(
                    format_notification(
                        "❌ 续期失败",
                        error="未点击到弹窗确认按钮",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )
                return

            # 点击后立即采集现场(不清空弹窗): 弹窗文案/Toast/网络响应/控制台报错
            sb.sleep(3)
            diag_snapshot(sb, "点击确认后")
            diag_network(sb, "点击确认后网络")
            diag_console(sb, "点击确认后控制台")

            # 服务端已明确拒绝(如 RENEW_AUTOMATED)时无需再空等 60 秒
            api_err = last_renew_api_error(sb, click_ts)
            if api_err:
                status = api_err.get("status")
                body = str(api_err.get("body") or "").strip()
                code = ""
                try:
                    code = json.loads(body).get("code", "")
                except Exception:
                    pass
                if status == 403 or code == "RENEW_AUTOMATED":
                    msg = f"服务端拒绝续期(HTTP {status}, {code or 'unknown'}): {body[:180]}"
                    print(f"❌ {msg}")
                    send_moviepilot_message(
                        format_notification(
                            "❌ 续期失败",
                            error=msg,
                            expiry_date=current_expiry or "（未获取到）"
                        )
                    )
                    return
            elif modal_button_clicked and js(
                sb,
                "var b=document.querySelector('div[role=\\\"dialog\\\"] button.btn-primary');"
                "return b ? !b.disabled : false;",
                False,
            ):
                # 3 秒内既没有续期请求也没有服务端错误, 且按钮仍可点: 说明点击没命中, 回退一次
                print("⚠️ 点击未触发续期请求, 回退 WebDriver 再点一次")
                try:
                    sb.click('div[role="dialog"] button.btn-primary', timeout=5)
                    sb.sleep(2)
                except Exception as e:
                    print(f"回退点击失败: {e}")

            print("⏳ 等待新的过期时间...")
            renew_ok, new_expiry, new_countdown, reason = confirm_renewal_result(sb, current_expiry)
            if renew_ok:
                print(f"✅ 续期成功（{reason}）")
                if new_countdown:
                    extra = f"⏱️ 可续期时间: {format_countdown(new_countdown)}后"
                else:
                    extra = reason
                send_moviepilot_message(
                    format_notification(
                        "✅ 续期成功",
                        extra=extra,
                        expiry_date=new_expiry or "（未获取到）"
                    )
                )
            else:
                print(f"❌ 续期失败（{reason}）")
                send_moviepilot_message(
                    format_notification(
                        "❌ 续期失败",
                        error=reason,
                        expiry_date=new_expiry or "（未获取到）"
                    )
                )

        else:
            if countdown_text:
                friendly = format_countdown(countdown_text)
                # 冷却中说明本期已经续期过，按成功告知
                print(f"✅ 本期已续期（冷却中，{friendly} 后可再次续期）")
                send_moviepilot_message(
                    format_notification(
                        "✅ 续期成功",
                        extra=f"本期已续期，{friendly}后可再次续期",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )
            else:
                print("❌ 未找到续期按钮或倒计时，判定续期失败")
                diag_snapshot(sb, "未找到续期按钮现场")
                diag_network(sb, "未找到续期按钮网络")
                send_moviepilot_message(
                    format_notification(
                        "❌ 续期失败",
                        error="未找到续期按钮，请检查账号状态或页面结构",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )

        print("🏁 脚本执行完毕")

if __name__ == "__main__":
    main()
