"""
Web 控制面板模块。

提供可视化控制页面（http://127.0.0.1:WEB_PORT），
支持启停/暂停/恢复桥接，显示运行状态和日志，
以及在线编辑 config.json 配置。
"""

import json
import logging
import math
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

_BRIDGE_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BRIDGE_MODULE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_MODULE_DIR)

import state
import config
from money_service import MoneyRequestError
from reply_store import ReplyStoreError, default_store
from uia_support import (
    CALIBRATION_REQUIRED,
    CALIBRATION_WINDOW,
    CalibrationError,
    Win32WeChatDriver,
    validate_calibration,
    validate_runtime_metrics,
)

log = logging.getLogger("ob11-bridge")

_CHAT_HISTORY_DEFAULT_LIMIT = 100
_CHAT_HISTORY_MAX_LIMIT = 200
_CHAT_LOG_READ_CHUNK = 65536
_CHAT_LOG_MAX_SCAN_BYTES = 4 * 1024 * 1024
_CHAT_REQUIRED_FIELDS = {"event", "scope", "contact", "status", "body"}
_CHAT_ALLOWED_FIELDS = _CHAT_REQUIRED_FIELDS | {"sender"}


def _parse_chat_log_line(line: str):
    marker = "] CHAT "
    marker_index = line.find(marker)
    if marker_index < 8:
        return None
    recorded_at = line[:8]
    if (
        len(recorded_at) != 8
        or recorded_at[2] != ":"
        or recorded_at[5] != ":"
        or not recorded_at.replace(":", "").isdigit()
    ):
        return None
    try:
        payload = json.loads(line[marker_index + len(marker) :])
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or not _CHAT_REQUIRED_FIELDS.issubset(payload)
        or not set(payload).issubset(_CHAT_ALLOWED_FIELDS)
        or any(not isinstance(value, str) for value in payload.values())
        or payload["event"] not in {"inbound", "outbound"}
        or payload["scope"] not in {"private", "group"}
        or payload["status"] not in {"received", "sent", "failed"}
    ):
        return None
    return {"time": recorded_at, **payload}


def _read_chat_records(path, limit: int = _CHAT_HISTORY_DEFAULT_LIMIT):
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _CHAT_HISTORY_MAX_LIMIT
    ):
        raise ValueError("invalid chat history limit")

    newest_first = []
    scanned = 0
    with open(path, "rb") as stream:
        stream.seek(0, 2)
        remaining = stream.tell()
        pending = b""
        while (
            remaining > 0
            and len(newest_first) < limit
            and scanned < _CHAT_LOG_MAX_SCAN_BYTES
        ):
            read_size = min(
                _CHAT_LOG_READ_CHUNK,
                remaining,
                _CHAT_LOG_MAX_SCAN_BYTES - scanned,
            )
            remaining -= read_size
            scanned += read_size
            stream.seek(remaining)
            pending = stream.read(read_size) + pending
            lines = pending.split(b"\n")
            pending = lines[0]
            for raw_line in reversed(lines[1:]):
                try:
                    parsed = _parse_chat_log_line(
                        raw_line.rstrip(b"\r").decode("utf-8")
                    )
                except UnicodeDecodeError:
                    parsed = None
                if parsed is not None:
                    newest_first.append(parsed)
                    if len(newest_first) == limit:
                        break
        if remaining == 0 and len(newest_first) < limit and pending:
            try:
                parsed = _parse_chat_log_line(
                    pending.rstrip(b"\r").decode("utf-8")
                )
            except UnicodeDecodeError:
                parsed = None
            if parsed is not None:
                newest_first.append(parsed)
    newest_first.reverse()
    return newest_first


def _local_endpoint(value: str):
    try:
        parsed = urlsplit("//" + value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if host not in {"127.0.0.1", "localhost"}:
        return None
    if port is not None and port != int(config.WEB_PORT):
        return None
    return host


def _request_is_local(handler, *, write: bool = False) -> bool:
    headers = handler.headers
    host = _local_endpoint(headers.get("Host", ""))
    if host is None:
        return False

    origin = headers.get("Origin", "")
    if origin:
        try:
            parsed_origin = urlsplit(origin)
            origin_port = parsed_origin.port
        except ValueError:
            return False
        if (
            parsed_origin.scheme != "http"
            or (parsed_origin.hostname or "").lower()
            not in {"127.0.0.1", "localhost"}
            or (origin_port if origin_port is not None else 80)
            != int(config.WEB_PORT)
        ):
            return False

    if write:
        fetch_site = headers.get("Sec-Fetch-Site", "")
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            return False
    return True


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>控制面板</title>
<link rel="icon" href="data:,">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#fff;--surface:#fff;--line:#e5e7eb;--line-strong:#d1d5db;--text:#111827;--muted:#6b7280;--soft:#f9fafb;--hover:#f3f4f6;--active:#111827}
body{font-family:-apple-system,'Segoe UI',sans-serif;background:#fff;height:100vh;color:var(--text);display:flex;margin:0;overflow:hidden}

/* ===== 主容器 ===== */
.container{display:flex;width:100vw;height:100vh;background:#fff;overflow:hidden;border:none}

/* ===== 侧边栏 ===== */
.sidebar{width:112px;min-width:112px;background:#fff;display:flex;flex-direction:column;align-items:center;padding:20px 0;gap:6px;border-right:1px solid var(--line);height:100vh}
.sidebar .logo{display:none}
.sidebar .nav-item{width:88px;height:40px;border-radius:6px;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:background .15s,color .15s,border-color .15s;color:var(--muted);font-size:13px;font-weight:500;gap:6px;border:1px solid transparent;background:transparent;padding:0 10px}
.sidebar .nav-item .icon{font-size:20px;line-height:1}
.sidebar .nav-item:hover{background:var(--hover);color:var(--text)}
.sidebar .nav-item.active{background:var(--active);color:#fff;box-shadow:none}
.sidebar .nav-item.active:hover{color:#fff}

/* ===== 内容区 ===== */
.content{flex:1;padding:28px 32px;overflow-y:auto;display:flex;flex-direction:column;gap:16px;height:100vh;background:#fff}
.content::-webkit-scrollbar{width:4px}
.content::-webkit-scrollbar-thumb{background:#d1d5db;border-radius:4px}

.tab-page{display:none;flex-direction:column;gap:16px;height:100%}
.tab-page.active{display:flex}

/* ===== 标题栏 ===== */
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:2px}
.header h1{font-size:24px;font-weight:650;display:flex;align-items:baseline;gap:10px;letter-spacing:0;color:var(--text)}
.header h1 .en{font-family:inherit;background:none;-webkit-text-fill-color:currentColor;color:var(--text);letter-spacing:0}
.header h1 .cn{font-family:inherit;font-size:24px;font-weight:650;background:none;-webkit-text-fill-color:currentColor;color:var(--text);letter-spacing:0}
.header .badge{font-size:11px;color:var(--muted);background:#fff;padding:3px 10px;border-radius:999px;font-weight:500;border:1px solid var(--line)}

/* ===== 状态卡片 ===== */
.status-row{display:flex;gap:8px;flex-wrap:wrap}
.status-card{flex:1;min-width:90px;background:#fff;border-radius:6px;padding:12px 14px;text-align:center;box-shadow:none;border:1px solid var(--line)}
.status-card .label{font-size:11px;color:var(--muted);margin-bottom:4px}
.status-card .value{font-size:15px;font-weight:600}
.status-card .value.online{color:#4caf50}
.status-card .value.offline{color:#bdbdbd}
.status-card .value.busy{color:#ff9800}

/* ===== 按钮组 ===== */
.btn-row{display:flex;gap:8px;flex-wrap:wrap}
.btn{padding:9px 16px;border:1px solid var(--line-strong);border-radius:6px;font-size:13px;font-weight:550;cursor:pointer;transition:background .15s,color .15s,border-color .15s;display:inline-flex;align-items:center;gap:6px;background:#fff;color:var(--text)}
.btn:disabled{opacity:0.35;cursor:not-allowed;filter:none!important}
.btn:active:not(:disabled){transform:none}
.btn:hover:not(:disabled){background:var(--hover);border-color:#9ca3af}
.btn-pink{background:#111827;color:#fff;border-color:#111827;box-shadow:none}
.btn-pink:hover:not(:disabled){background:#000;border-color:#000}
.btn-green{background:#111827;color:#fff;border-color:#111827;box-shadow:none}
.btn-green:hover:not(:disabled){background:#000;border-color:#000}
.btn-red{background:#fff;color:#991b1b;border-color:#fecaca;box-shadow:none}
.btn-red:hover:not(:disabled){background:#fef2f2;border-color:#fca5a5}
.btn-amber{background:#fff;color:#92400e;border-color:#fde68a;box-shadow:none}
.btn-amber:hover:not(:disabled){background:#fffbeb;border-color:#fcd34d}
.btn-outline{background:#fff;color:var(--text);border:1px solid var(--line-strong)}
.btn-outline:hover:not(:disabled){background:var(--hover);border-color:#9ca3af}

/* ===== 模式行 ===== */
.mode-row{display:flex;align-items:center;gap:10px;font-size:13px;color:var(--muted);flex-wrap:wrap}
.mode-row .mode-value{font-weight:600;color:var(--text)}

/* ===== 发送预览 ===== */
.send-preview{display:none;border:1px solid #fde68a;background:#fffbeb;border-radius:6px;padding:12px 14px}
.send-preview.active{display:block}
.send-preview-title{font-size:12px;font-weight:650;color:#92400e;margin-bottom:7px}
.send-preview-content{font-size:14px;line-height:1.6;color:#111827;white-space:pre-wrap;overflow-wrap:anywhere;max-height:180px;overflow-y:auto}
.send-result{display:none;border:1px solid var(--line);background:var(--soft);border-radius:6px;padding:10px 14px}
.send-result.active{display:block}
.send-result.failed{border-color:#fecaca;background:#fef2f2;color:#991b1b}
.send-result.sent{border-color:#bbf7d0;background:#f0fdf4;color:#166534}
.send-result-title{font-size:12px;font-weight:650;margin-bottom:4px}
.send-result-message{font-size:13px;line-height:1.5}

/* ===== 故障恢复 ===== */
.recovery-intro{font-size:13px;line-height:1.65;color:#374151;background:var(--soft);border:1px solid var(--line);border-radius:6px;padding:12px 14px}
.recovery-panel{border:1px solid var(--line);border-radius:6px;background:#fff;padding:14px}
.recovery-panel h3{font-size:14px;font-weight:650;margin-bottom:5px}
.recovery-panel .panel-note{font-size:12px;color:var(--muted);line-height:1.55;margin-bottom:12px}
.recovery-actions{display:flex;gap:8px;flex-wrap:wrap}
.recovery-list{display:flex;flex-direction:column;gap:8px}
.recovery-empty{font-size:13px;color:#047857;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;padding:12px}
.recovery-item{border:1px solid var(--line);border-radius:6px;padding:12px;background:#fff}
.recovery-item.blocked{border-color:#fecaca;background:#fffafa}
.recovery-item.waiting{border-color:#fde68a;background:#fffdf7}
.recovery-item-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:8px}
.recovery-contact{font-size:14px;font-weight:650;overflow-wrap:anywhere}
.recovery-id{font-size:11px;color:var(--muted);font-weight:400;margin-top:2px}
.recovery-age{font-size:11px;color:var(--muted);white-space:nowrap}
.issue-list{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:9px}
.issue-chip{font-size:11px;color:#374151;background:#f3f4f6;border:1px solid var(--line);border-radius:999px;padding:3px 8px}
.issue-chip.blocker{color:#991b1b;background:#fef2f2;border-color:#fecaca}
.issue-chip.warning{color:#92400e;background:#fffbeb;border-color:#fde68a}
.diagnostic-lines{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}
.diagnostic-line{font-size:12px;line-height:1.5;border:1px solid var(--line);border-radius:6px;padding:9px 10px;background:var(--soft)}
.diagnostic-line strong{display:block;font-size:11px;color:var(--muted);margin-bottom:2px}
.recovery-scroll{overflow-y:auto;padding-right:3px;display:flex;flex-direction:column;gap:12px}

/* ===== 日志 ===== */
.log-box{flex:1;min-height:100px;background:#fff;border:1px solid var(--line);border-radius:6px;padding:12px;font-size:12px;font-family:'Cascadia Code','Fira Code',monospace;color:#374151;overflow-y:auto;line-height:1.6;white-space:pre-wrap}
.log-box:empty::before{content:'暂无聊天记录';color:#9ca3af}
.log-box::-webkit-scrollbar{width:4px}
.log-box::-webkit-scrollbar-thumb{background:#d1d5db;border-radius:4px}
.chat-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;font-size:13px;font-weight:650}
.chat-heading .privacy-note{font-size:11px;font-weight:400;color:var(--muted)}
.chat-entry{padding:10px 0;border-bottom:1px solid var(--line)}
.chat-entry:last-child{border-bottom:none}
.chat-meta{font-family:-apple-system,'Segoe UI',sans-serif;font-size:12px;font-weight:650;color:var(--text);white-space:pre-wrap;overflow-wrap:anywhere}
.chat-meta.outbound{color:#047857}
.chat-meta.failed{color:#b91c1c}
.chat-body{margin-top:4px;font-family:-apple-system,'Segoe UI',sans-serif;font-size:14px;line-height:1.55;color:#1f2937;white-space:pre-wrap;overflow-wrap:anywhere}

/* ===== 设置页面 ===== */
.settings-scroll{flex:1;overflow-y:auto;padding-right:4px}
.settings-scroll::-webkit-scrollbar{width:4px}
.settings-scroll::-webkit-scrollbar-thumb{background:#d1d5db;border-radius:4px}
.settings-group{margin-bottom:18px}
.settings-group h3{font-size:13px;font-weight:600;color:var(--text);margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--line)}
.settings-row{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:6px}
.settings-field{flex:1;min-width:160px}
.settings-field label{display:block;font-size:11px;color:var(--muted);margin-bottom:3px;font-weight:500}
.settings-field input,.settings-field select,.settings-field textarea{width:100%;padding:7px 10px;border:1px solid var(--line-strong);border-radius:6px;font-size:12px;outline:none;transition:border .15s,box-shadow .15s;background:#fff;color:var(--text);font-family:inherit}
.settings-field input:focus,.settings-field select:focus,.settings-field textarea:focus{border-color:#111827;box-shadow:0 0 0 2px rgba(17,24,39,0.08)}
.settings-field textarea{resize:vertical;min-height:36px}
.settings-field select{cursor:pointer;appearance:auto;padding-right:10px}

/* ===== 保存按钮 ===== */
.save-bar{display:flex;justify-content:flex-end;align-items:center;gap:12px;padding-top:8px;border-top:1px solid var(--line)}
.save-bar .save-msg{font-size:12px;color:#66bb6a;opacity:0;transition:opacity .4s}
.save-bar .save-msg.show{opacity:1}

/* ===== Toast ===== */
.toast{position:fixed;top:24px;left:50%;transform:translateX(-50%);padding:10px 24px;border-radius:6px;font-size:13px;font-weight:500;z-index:999;opacity:0;transition:opacity .4s;pointer-events:none;box-shadow:none;border:1px solid var(--line)}
.toast.show{opacity:1}
.toast.success{background:#e8f5e9;color:#2e7d32;border:1px solid #c8e6c9}
.toast.error{background:#ffebee;color:#c62828;border:1px solid #ffcdd2}
.toast.info{background:#fff;color:var(--text);border:1px solid var(--line)}
</style>
</head>
<body>

<div class="toast" id="toast"></div>

<div class="container">

<!-- ===== 侧边栏 ===== -->
<div class="sidebar">
  <button class="nav-item active" data-tab="dashboard" onclick="switchTab('dashboard')">
    <span>控制面板</span>
  </button>
  <button class="nav-item" data-tab="recovery" onclick="switchTab('recovery')">
    <span>故障恢复</span>
  </button>
  <button class="nav-item" data-tab="settings" onclick="switchTab('settings')">
    <span>基础设置</span>
  </button>
</div>

<!-- ===== 内容区 ===== -->
<div class="content">

  <!-- ===== 面板页 ===== -->
  <div class="tab-page active" id="page-dashboard">
    <div class="header">
      <h1>控制面板</h1>
      <div class="badge" id="statusText">加载中...</div>
    </div>

    <div class="status-row">
      <div class="status-card"><div class="label">桥接状态</div><div class="value" id="bridgeStatus">-</div></div>
      <div class="status-card"><div class="label">AstrBot</div><div class="value" id="obStatus">-</div></div>
      <div class="status-card"><div class="label">WeFlow</div><div class="value" id="weflowStatus">-</div></div>
      <div class="status-card"><div class="label">发送模式</div><div class="value" id="sendMethod" style="font-size:13px">-</div></div>
    </div>

    <div class="send-preview" id="sendPreview">
      <div class="send-preview-title" id="sendPreviewTitle">即将粘贴</div>
      <div class="send-preview-content" id="sendPreviewContent"></div>
    </div>

    <div class="send-result" id="sendResult" role="status" aria-live="polite">
      <div class="send-result-title" id="sendResultTitle"></div>
      <div class="send-result-message" id="sendResultMessage"></div>
      <button class="btn btn-outline" id="btnDismissResult" onclick="dismissLastResult()" style="display:none;margin-top:8px;padding:5px 10px;font-size:11px">清除此条历史提示</button>
    </div>

    <div class="send-result" id="draftQuarantinePanel" role="status" aria-live="polite">
      <div class="send-result-title">草稿内容发生变化，已暂停该联系人自动回复</div>
      <div class="send-result-message" id="draftQuarantines"></div>
    </div>

    <div class="send-result" id="commitUnknownPanel" role="status" aria-live="polite">
      <div class="send-result-title">发送按钮已触发，但无法确认微信是否提交</div>
      <div class="send-result-message" id="commitUnknownRows"></div>
    </div>

    <div class="btn-row">
      <button class="btn btn-pink" id="btnStart" onclick="action('start')">启动</button>
      <button class="btn btn-red" id="btnStop" onclick="action('stop')" disabled>停止</button>
      <button class="btn btn-amber" id="btnPause" onclick="action('pause')" disabled>暂停</button>
      <button class="btn btn-green" id="btnResume" onclick="action('resume')" style="display:none" disabled>恢复</button>
      <button class="btn btn-red" id="btnCancelCurrent" onclick="cancelCurrentSend()" disabled>取消此条</button>
    </div>

    <div class="mode-row">
      <span>群聊模式:</span>
      <span class="mode-value" id="modeStatus">-</span>
      <button class="btn btn-outline" id="btnToggleMode" style="padding:5px 14px;font-size:12px">切换</button>
    </div>

    <div class="chat-heading">
      <span>完整聊天记录</span>
      <span class="privacy-note">包含联系人名称和收发正文，仅限本机查看</span>
    </div>
    <div class="log-box" id="chatHistory"></div>
  </div>

  <!-- ===== 故障恢复页 ===== -->
  <div class="tab-page" id="page-recovery">
    <div class="header">
      <h1>故障恢复</h1>
      <div class="badge" id="recoveryBadge">检测中...</div>
    </div>

    <div class="recovery-intro">
      这里区分“历史发送提示”和“仍在阻塞的状态”。草稿隔离与发送结果未知不会因重启自动消失，必须由你核对微信后精确确认；租约可以安全地重新握手，卡住的联系人任务只能在桥接暂停时释放。
    </div>

    <div class="status-row">
      <div class="status-card"><div class="label">微信窗口</div><div class="value" id="recoveryWindow">-</div></div>
      <div class="status-card"><div class="label">合并回复能力</div><div class="value" id="recoveryCapability">-</div></div>
      <div class="status-card"><div class="label">阻塞联系人</div><div class="value" id="recoveryBlocked">-</div></div>
      <div class="status-card"><div class="label">待处理批次</div><div class="value" id="recoveryPending">-</div></div>
    </div>

    <div class="recovery-scroll">
      <div class="recovery-panel">
        <h3>恢复操作</h3>
        <div class="panel-note">微信窗口状态仅作只读检测；如未就绪，请回到微信手动恢复、置前并最大化。重新握手不会重发任何已经提交的回复。</div>
        <div class="recovery-actions">
          <button class="btn btn-outline" onclick="restartMergedHandshake()">重新建立合并回复能力</button>
          <button class="btn btn-outline" onclick="dismissLastResult()">清除历史发送提示</button>
          <button class="btn btn-outline" onclick="refreshRecovery()">立即重新检测</button>
        </div>
      </div>

      <div class="recovery-panel">
        <h3>联系人恢复</h3>
        <div class="panel-note">红色项目需要人工核对。黄色项目是仍在排队或处理中；释放卡住任务前请先点击主面板“暂停”。</div>
        <div class="recovery-list" id="recoveryContacts"></div>
      </div>

      <div class="recovery-panel">
        <h3>运行诊断</h3>
        <div class="panel-note">仅显示数量和状态，不显示消息正文、微信账号、会话 ID、租约或 admission 凭据。</div>
        <div class="diagnostic-lines" id="recoveryDiagnostics"></div>
      </div>
    </div>
  </div>

  <!-- ===== 设置页 ===== -->
  <div class="tab-page" id="page-settings">
    <div class="header">
      <h1>配置编辑</h1>
      <div class="badge">config.json</div>
    </div>

    <div class="settings-scroll" id="settingsForm">
      <!-- 由 JS 动态渲染 -->
    </div>

    <div class="save-bar">
      <span class="save-msg" id="saveMsg">✅ 已保存</span>
      <button class="btn btn-pink" onclick="saveConfig()">保存配置</button>
    </div>
  </div>

</div>
</div>

<script>
// ===== 工具 =====
function toast(msg, type) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + type + ' show';
  setTimeout(function(){t.className='toast'}, 2500);
}

function showMsg(text) {
  var el = document.getElementById('saveMsg');
  el.textContent = text;
  el.className = 'save-msg show';
  setTimeout(function(){el.className='save-msg'}, 2500);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, function(character) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character];
  });
}

// ===== Tab 切换 =====
function switchTab(name) {
  document.querySelectorAll('.tab-page').forEach(function(p){p.classList.remove('active')});
  document.getElementById('page-' + name).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active')});
  document.querySelector('[data-tab="' + name + '"]').classList.add('active');
  if (name === 'settings') loadConfig();
  if (name === 'recovery') refreshRecovery();
}

// ===== 面板刷新 =====
var modeMap = {'mention':'仅@回复','all':'全部回复','batch':'批处理'};
var visiblePreviewId = null;

function refreshDashboard() {
  fetch('/status').then(function(r){return r.json()}).then(function(s){
    var st = document.getElementById('bridgeStatus');
    if (!s.running) { st.textContent='未运行'; st.style.color='#bdbdbd';
    } else if (s.paused) { st.textContent='已暂停'; st.style.color='#ff9800';
    } else { st.textContent='运行中'; st.style.color='#4caf50'; }

    document.getElementById('statusText').textContent = s.running ? (s.paused ? '已暂停' : '运行中') : '未运行';
    document.getElementById('obStatus').textContent = s.ob_connected ? '已连接' : '未连接';
    document.getElementById('obStatus').style.color = s.ob_connected ? '#4caf50' : '#bdbdbd';
    document.getElementById('weflowStatus').textContent = s.weflow_connected ? '已连接' : '未连接';
    document.getElementById('weflowStatus').style.color = s.weflow_connected ? '#4caf50' : '#bdbdbd';
    document.getElementById('sendMethod').textContent = s.sender_mode + (s.calibrated ? '（已标定）' : '（待标定）') + (s.merged_reply_ready ? ' · 合并就绪' : ' · 合并未就绪');

    var preview = s.send_preview;
    var previewPanel = document.getElementById('sendPreview');
    if (preview && typeof preview.content === 'string') {
      visiblePreviewId =
        preview.stage !== 'submitting' && Number.isInteger(preview.preview_id)
          ? preview.preview_id
          : null;
      previewPanel.classList.add('active');
      var isFavoriteSticker = preview.message_type === 'favorite_sticker';
      var previewTitle = isFavoriteSticker ? '即将发送收藏表情' : '即将粘贴';
      if (preview.stage === 'pasted_waiting') {
        previewTitle = isFavoriteSticker ? '等待发送收藏表情' : '已粘贴，等待发送';
      }
      if (preview.stage === 'paused') previewTitle = '已暂停，将从此条继续';
      if (preview.stage === 'submitting') previewTitle = '正在发送';
      if (typeof preview.remaining_seconds === 'number' && preview.stage !== 'submitting') {
        previewTitle += '（约 ' + preview.remaining_seconds.toFixed(1) + ' 秒）';
      }
      if (typeof preview.contact === 'string' && preview.contact) {
        previewTitle += ' → ' + preview.contact;
      }
      document.getElementById('sendPreviewTitle').textContent = previewTitle;
      document.getElementById('sendPreviewContent').textContent = preview.content;
    } else {
      visiblePreviewId = null;
      previewPanel.classList.remove('active');
      document.getElementById('sendPreviewContent').textContent = '';
    }
    document.getElementById('btnCancelCurrent').disabled = visiblePreviewId === null;

    var outcome = s.last_send_result;
    var resultPanel = document.getElementById('sendResult');
    if (
      outcome
      && (outcome.status === 'failed' || outcome.status === 'sent')
      && typeof outcome.message === 'string'
    ) {
      resultPanel.className = 'send-result active ' + outcome.status;
      document.getElementById('sendResultTitle').textContent =
        outcome.status === 'failed' ? '最近一次发送失败（历史提示）' : '最近一次发送已提交';
      document.getElementById('sendResultMessage').textContent = outcome.message;
      document.getElementById('btnDismissResult').style.display = 'inline-flex';
    } else {
      resultPanel.className = 'send-result';
      document.getElementById('sendResultTitle').textContent = '';
      document.getElementById('sendResultMessage').textContent = '';
      document.getElementById('btnDismissResult').style.display = 'none';
    }

    document.getElementById('btnStart').disabled = s.running;
    document.getElementById('btnStop').disabled = !s.running;
    if (s.paused) {
      document.getElementById('btnPause').style.display = 'none';
      document.getElementById('btnResume').style.display = 'inline-block';
      document.getElementById('btnResume').disabled = false;
    } else {
      document.getElementById('btnPause').style.display = 'inline-block';
      document.getElementById('btnPause').disabled = !s.running;
      document.getElementById('btnResume').style.display = 'none';
    }

    document.getElementById('modeStatus').textContent = modeMap[s.group_reply_mode] || s.group_reply_mode;

  });
}

function refreshDraftQuarantines() {
  fetch('/draft-quarantines').then(function(r){return r.json()}).then(function(data){
    var rows = Array.isArray(data.quarantines) ? data.quarantines : [];
    var panel = document.getElementById('draftQuarantinePanel');
    var body = document.getElementById('draftQuarantines');
    if (!rows.length) {
      panel.className = 'send-result';
      body.textContent = '';
      return;
    }
    panel.className = 'send-result active failed';
    while (body.firstChild) body.removeChild(body.firstChild);
    rows.forEach(function(row){
      var line = document.createElement('div');
      line.textContent = (row.routing_name || ('目标 ' + row.target_id)) + '：请先人工检查微信输入框。 ';
      var button = document.createElement('button');
      button.className = 'btn btn-outline';
      button.textContent = '我已人工检查，恢复此联系人';
      button.onclick = function(){
        fetch('/resolve-draft-quarantine', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({target_id:row.target_id,request_id:row.request_id}),
        }).then(function(r){return r.json()}).then(function(){refreshDraftQuarantines();});
      };
      line.appendChild(button);
      body.appendChild(line);
    });
  });
}

function refreshCommitUnknown() {
  fetch('/commit-unknown').then(function(r){return r.json()}).then(function(data){
    var rows = Array.isArray(data.rows) ? data.rows : [];
    var panel = document.getElementById('commitUnknownPanel');
    var body = document.getElementById('commitUnknownRows');
    if (!rows.length) {
      panel.className = 'send-result';
      body.replaceChildren();
      return;
    }
    panel.className = 'send-result active failed';
    body.replaceChildren();
    rows.forEach(function(row){
      var line = document.createElement('div');
      line.textContent = (row.routing_name || ('目标 ' + row.target_id)) + '：请查看微信聊天记录后确认。 ';
      [['已发送','sent'],['未发送','not_sent']].forEach(function(choice){
        var button = document.createElement('button');
        button.className = 'btn btn-outline';
        button.textContent = choice[0];
        button.onclick = function(){
          fetch('/resolve-commit-unknown', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({
              request_id:row.request_id,
              revision:row.result_revision,
              resolution:choice[1]
            }),
          }).then(function(r){return r.json()}).then(function(){refreshCommitUnknown();});
        };
        line.appendChild(button);
      });
      body.appendChild(line);
    });
  });
}

// ===== 故障恢复 =====
var recoveryRefreshBusy = false;
var recoveryErrorMap = {
  'E_RECOVERY_PAUSE_REQUIRED':'请先在控制面板暂停桥接，再释放卡住的联系人任务',
  'E_RECOVERY_DRAFT_REVIEW_REQUIRED':'请先检查该联系人的微信输入框并解除草稿隔离',
  'E_RECOVERY_COMMIT_REVIEW_REQUIRED':'请先确认这条回复在微信中到底是否已经发送',
  'E_RECOVERY_JOB_BUSY':'该联系人仍有发送任务执行中，暂时不能释放',
  'E_RECOVERY_MERGED_OFFLINE':'AstrBot 尚未连接，当前无法重新握手',
  'E_UIA_CALIBRATION_REQUIRED':'尚未完成固定坐标标定',
  'E_UIA_CALIBRATION_INVALID':'固定坐标标定数据无效',
  'E_UIA_CALIBRATION_WINDOW':'未找到符合要求的微信主窗口',
  'E_UIA_RECALIBRATION_REQUIRED':'窗口尺寸或 DPI 已变化，需要重新标定',
  'E_RECOVERY_READ':'恢复状态读取失败',
  'E_RECOVERY_REQUEST':'恢复请求无效'
};

function recoveryMessage(code) {
  return recoveryErrorMap[String(code || '')] || ('操作失败：' + String(code || '未知错误'));
}

function recoveryPost(path, payload) {
  return fetch(path, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload || {})
  }).then(function(response){
    return response.json().then(function(result){
      if (!response.ok || result.ok === false) {
        throw new Error(result.error || 'E_RECOVERY_REQUEST');
      }
      return result;
    });
  });
}

function formatRecoveryTime(value) {
  if (typeof value !== 'number' || !isFinite(value)) return '';
  var elapsed = Math.max(0, Date.now() / 1000 - value);
  if (elapsed < 60) return Math.floor(elapsed) + ' 秒前';
  if (elapsed < 3600) return Math.floor(elapsed / 60) + ' 分钟前';
  if (elapsed < 86400) return Math.floor(elapsed / 3600) + ' 小时前';
  return Math.floor(elapsed / 86400) + ' 天前';
}

function appendIssue(container, label, kind) {
  var chip = document.createElement('span');
  chip.className = 'issue-chip' + (kind ? ' ' + kind : '');
  chip.textContent = label;
  container.appendChild(chip);
}

function appendRecoveryButton(container, label, click, className) {
  var button = document.createElement('button');
  button.className = 'btn ' + (className || 'btn-outline');
  button.style.padding = '6px 10px';
  button.style.fontSize = '11px';
  button.textContent = label;
  button.onclick = click;
  container.appendChild(button);
}

function resolveDraftRecovery(row) {
  var name = row.routing_name || ('目标 ' + row.target_id);
  if (!confirm('请先打开微信检查“' + name + '”的输入框。\\n\\n确认其中没有需要保留的草稿后，才可继续。')) return;
  recoveryPost('/resolve-draft-quarantine', {
    target_id:row.target_id,
    request_id:row.quarantine.request_id
  }).then(function(result){
    if (!result.resolved) throw new Error('E_RECOVERY_REQUEST');
    toast('已解除该联系人的草稿隔离', 'success');
    refreshDraftQuarantines();
    refreshRecovery();
  }).catch(function(error){toast(recoveryMessage(error.message), 'error')});
}

function resolveCommitRecovery(row, item, resolution) {
  var name = row.routing_name || ('目标 ' + row.target_id);
  var prompt = resolution === 'sent'
    ? '确认微信聊天记录中已经出现这条 Bot 回复？\\n\\n确认后系统会把它记为已发送，不会重试。'
    : '确认微信聊天记录中没有这条 Bot 回复？\\n\\n确认后系统会把它记为失败，也不会自动重试。';
  if (!confirm(name + '\\n\\n' + prompt)) return;
  recoveryPost('/resolve-commit-unknown', {
    request_id:item.request_id,
    revision:item.revision,
    resolution:resolution
  }).then(function(result){
    if (!result.resolved) throw new Error('E_RECOVERY_REQUEST');
    toast('发送结果已确认', 'success');
    refreshCommitUnknown();
    refreshRecovery();
  }).catch(function(error){toast(recoveryMessage(error.message), 'error')});
}

function releaseContactRecovery(row) {
  var name = row.routing_name || ('目标 ' + row.target_id);
  if (!confirm('释放“' + name + '”卡住的发送许可？\\n\\n请先暂停桥接。此操作不会重发已经提交的消息；如果没有待处理批次，需要让联系人再发一条新消息。')) return;
  recoveryPost('/api/recovery/contact', {target_id:row.target_id}).then(function(result){
    toast('已释放 ' + result.released_admissions + ' 个卡住任务，重新排队 ' + result.requeued_batches + ' 个批次', 'success');
    refreshRecovery();
  }).catch(function(error){toast(recoveryMessage(error.message), 'error')});
}

function renderRecoveryContacts(rows) {
  var list = document.getElementById('recoveryContacts');
  list.replaceChildren();
  if (!rows.length) {
    var empty = document.createElement('div');
    empty.className = 'recovery-empty';
    empty.textContent = '没有发现被隔离、结果未知或卡住的联系人。';
    list.appendChild(empty);
    return;
  }
  rows.forEach(function(row){
    var blocked = !!row.quarantine || (Array.isArray(row.commit_unknown) && row.commit_unknown.length > 0);
    var item = document.createElement('div');
    item.className = 'recovery-item ' + (blocked ? 'blocked' : 'waiting');

    var head = document.createElement('div');
    head.className = 'recovery-item-head';
    var identity = document.createElement('div');
    var contact = document.createElement('div');
    contact.className = 'recovery-contact';
    contact.textContent = row.routing_name || '未知联系人';
    var target = document.createElement('div');
    target.className = 'recovery-id';
    target.textContent = '本机目标 ID：' + row.target_id;
    identity.appendChild(contact);
    identity.appendChild(target);
    var age = document.createElement('div');
    age.className = 'recovery-age';
    var oldest = row.quarantine ? row.quarantine.created_at : row.oldest_pending_batch_at;
    if (!oldest && row.commit_unknown.length) oldest = row.commit_unknown[0].updated_at;
    if (!oldest && row.active_admissions.length) oldest = row.active_admissions[0].created_at;
    if (!oldest && row.open_jobs.length) oldest = row.open_jobs[0].created_at;
    age.textContent = formatRecoveryTime(oldest);
    head.appendChild(identity);
    head.appendChild(age);
    item.appendChild(head);

    var issues = document.createElement('div');
    issues.className = 'issue-list';
    if (row.quarantine) appendIssue(issues, '草稿隔离：需检查输入框', 'blocker');
    if (row.commit_unknown.length) appendIssue(issues, row.commit_unknown.length + ' 条发送结果未知', 'blocker');
    if (row.active_admissions.length) appendIssue(issues, row.active_admissions.length + ' 个发送许可未结束', 'warning');
    if (row.open_jobs.length) {
      var stages = Array.from(new Set(row.open_jobs.map(function(job){return String(job.stage || job.state || 'unknown')})));
      appendIssue(issues, row.open_jobs.length + ' 个任务处理中：' + stages.join('、'), 'warning');
    }
    if (row.pending_batches) appendIssue(issues, row.pending_batches + ' 个消息批次待推送', 'warning');
    item.appendChild(issues);

    var actions = document.createElement('div');
    actions.className = 'recovery-actions';
    if (row.quarantine) {
      appendRecoveryButton(actions, '已检查输入框，解除隔离', function(){resolveDraftRecovery(row)}, 'btn-red');
    }
    row.commit_unknown.forEach(function(unknown){
      appendRecoveryButton(actions, '确认已发送', function(){resolveCommitRecovery(row, unknown, 'sent')}, 'btn-outline');
      appendRecoveryButton(actions, '确认未发送', function(){resolveCommitRecovery(row, unknown, 'not_sent')}, 'btn-outline');
    });
    if (
      row.active_admissions.length
      && !row.open_jobs.length
      && !row.quarantine
      && !row.commit_unknown.length
    ) {
      appendRecoveryButton(actions, '释放卡住任务', function(){releaseContactRecovery(row)}, 'btn-amber');
    }
    item.appendChild(actions);
    list.appendChild(item);
  });
}

function appendDiagnostic(container, title, value) {
  var line = document.createElement('div');
  line.className = 'diagnostic-line';
  var heading = document.createElement('strong');
  heading.textContent = title;
  var body = document.createElement('span');
  body.textContent = value;
  line.appendChild(heading);
  line.appendChild(body);
  container.appendChild(line);
}

function renderRecovery(data) {
  var summary = data.summary || {};
  var windowState = data.window || {};
  var capability = data.capability || {};
  var windowEl = document.getElementById('recoveryWindow');
  windowEl.textContent = windowState.ready ? '前台最大化' : (windowState.found ? '需要置前' : '未找到');
  windowEl.style.color = windowState.ready ? '#4caf50' : (windowState.found ? '#ff9800' : '#b91c1c');
  var capabilityEl = document.getElementById('recoveryCapability');
  capabilityEl.textContent = capability.ready ? '已就绪' : (capability.valid ? '恢复中' : '未就绪');
  capabilityEl.style.color = capability.ready ? '#4caf50' : (capability.valid ? '#ff9800' : '#b91c1c');
  document.getElementById('recoveryBlocked').textContent = String(summary.blocked_contacts || 0);
  document.getElementById('recoveryBlocked').style.color = summary.blocked_contacts ? '#b91c1c' : '#4caf50';
  document.getElementById('recoveryPending').textContent = String(summary.pending_batches || 0);
  document.getElementById('recoveryPending').style.color = summary.pending_batches ? '#ff9800' : '#4caf50';
  document.getElementById('recoveryBadge').textContent = summary.blocked_contacts ? '需要人工处理' : '无人工阻塞';

  renderRecoveryContacts(Array.isArray(data.targets) ? data.targets : []);
  var diagnostics = document.getElementById('recoveryDiagnostics');
  diagnostics.replaceChildren();
  appendDiagnostic(diagnostics, '桥接', data.running ? (data.paused ? '运行中 · 已暂停' : '运行中') : '未运行');
  appendDiagnostic(diagnostics, '能力租约', capability.ready ? ('有效 · 约 ' + Number(capability.expires_in_seconds || 0).toFixed(1) + ' 秒后续租') : String(capability.phase || '未连接'));
  appendDiagnostic(diagnostics, '发送队列', (summary.open_jobs || 0) + ' 个执行中 · ' + (summary.active_admissions || 0) + ' 个许可');
  appendDiagnostic(diagnostics, '持久批次', (summary.pending_batches || 0) + ' 个等待推送');
  appendDiagnostic(diagnostics, '微信窗口', windowState.ready ? '检查通过' : recoveryMessage(windowState.error_code));
  var last = data.last_send_result;
  appendDiagnostic(diagnostics, '最近发送记录', last && last.message ? last.message : '无历史提示');
}

function refreshRecovery() {
  if (recoveryRefreshBusy) return;
  recoveryRefreshBusy = true;
  fetch('/api/recovery').then(function(response){
    return response.json().then(function(result){
      if (!response.ok || result.error) throw new Error(result.error || 'E_RECOVERY_READ');
      return result;
    });
  }).then(renderRecovery).catch(function(error){
    document.getElementById('recoveryBadge').textContent = '检测失败';
    toast(recoveryMessage(error.message), 'error');
  }).finally(function(){recoveryRefreshBusy = false});
}

function restartMergedHandshake() {
  if (!confirm('重新建立合并回复能力？\\n\\n系统只会作废当前短期租约并重新注册，不会删除任务或重发消息。')) return;
  recoveryPost('/api/recovery/rehandshake', {}).then(function(){
    toast('已请求重新握手，通常会在数秒内恢复', 'success');
    setTimeout(refreshRecovery, 1500);
  }).catch(function(error){toast(recoveryMessage(error.message), 'error')});
}

function dismissLastResult() {
  recoveryPost('/api/recovery/dismiss-result', {}).then(function(){
    toast('历史发送提示已清除', 'success');
    refreshDashboard();
    refreshRecovery();
  }).catch(function(error){toast(recoveryMessage(error.message), 'error')});
}

function refreshChatHistory() {
  fetch('/api/chat-history?limit=100').then(function(r){
    if (!r.ok) throw new Error('E_CHAT_HISTORY_READ');
    return r.json();
  }).then(function(result){
    var history = document.getElementById('chatHistory');
    var isAtBottom = history.scrollHeight - history.scrollTop - history.clientHeight < 40;
    var fragment = document.createDocumentFragment();
    (Array.isArray(result.records) ? result.records : []).forEach(function(record){
      var entry = document.createElement('div');
      entry.className = 'chat-entry';

      var meta = document.createElement('div');
      meta.className = 'chat-meta ' + (record.event === 'outbound' ? 'outbound' : 'inbound');
      if (record.status === 'failed') meta.className += ' failed';
      var direction = record.event === 'outbound' ? 'Bot 发出 → ' : '收到 ← ';
      var identity = String(record.contact || '未知联系人');
      if (record.scope === 'group' && record.sender) {
        identity += ' · ' + String(record.sender);
      }
      var status = record.status === 'failed' ? '（失败）' : '';
      meta.textContent = String(record.time || '') + '  ' + direction + identity + status;

      var body = document.createElement('div');
      body.className = 'chat-body';
      body.textContent = String(record.body === undefined ? '' : record.body);

      entry.appendChild(meta);
      entry.appendChild(body);
      fragment.appendChild(entry);
    });
    history.replaceChildren(fragment);
    if (isAtBottom) history.scrollTop = history.scrollHeight;
  }).catch(function(){
    var history = document.getElementById('chatHistory');
    if (!history.childNodes.length) history.textContent = '聊天记录读取失败';
  });
}

function action(cmd) {
  fetch('/' + cmd, {method:'POST'}).then(function(){setTimeout(refreshDashboard,500)});
}

function cancelCurrentSend() {
  if (!Number.isInteger(visiblePreviewId)) return;
  var requestedPreviewId = visiblePreviewId;
  document.getElementById('btnCancelCurrent').disabled = true;
  fetch('/cancel-current', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({preview_id:requestedPreviewId}),
  }).then(function(r){return r.json()}).then(function(result){
    toast(result.cancelled ? '已取消此条，后续消息不受影响' : '此条已变化或已开始发送', result.cancelled ? 'success' : 'info');
    refreshDashboard();
  });
}

document.getElementById('btnToggleMode').onclick = function(){
  fetch('/mode', {method:'POST'}).then(function(){setTimeout(refreshDashboard,500)});
};

// ===== 设置加载 =====
function loadConfig() {
  fetch('/api/config').then(function(r){return r.json()}).then(function(cfg){
    renderConfigForm(cfg);
  }).catch(function(e){
    document.getElementById('settingsForm').innerHTML = '<p style="color:#e57373;font-size:13px;">加载配置失败: ' + e.message + '</p>';
  });
}

function renderConfigForm(cfg) {
  var html = '';
  var groups = [
    {title:'WeFlow 连接', fields:[
      {key:'weflow_base_url', label:'WeFlow 地址', type:'text', ph:'http://127.0.0.1:5031'},
      {key:'access_token', label:'Access Token', type:'password', ph:'输入Token'},
    ]},
    {title:'机器人', fields:[
      {key:'bot_nicknames', label:'机器人昵称（多个用逗号隔开）', type:'text', ph:'山山酱(^'},
      {key:'bot_wxid', label:'机器人 wxid', type:'text', ph:'wxid_xxx'},
    ]},
    {title:'AstrBot 连接', fields:[
      {key:'astrbot_ob_url', label:'AstrBot OB 地址', type:'text', ph:'ws://127.0.0.1:11229/ws'},
      {key:'astrbot_attachments', label:'附件目录（AstrBot 存放图片的路径）', type:'text', ph:'C:\\astrbot\\attachments'},
    ]},
    {title:'桥接设置', fields:[
      {key:'buffer_quiet_seconds', label:'消息静默合并窗口(秒)', type:'number', ph:'1.5'},
      {key:'buffer_max_seconds', label:'单批最长等待(秒)', type:'number', ph:'5'},
      {key:'group_reply_mode', label:'群聊回复模式', type:'select', opts:[{v:'mention',l:'仅@回复'},{v:'all',l:'全部回复'},{v:'batch',l:'批处理'}]},
      {key:'web_port', label:'Web 面板端口', type:'number', ph:'8766'},
      {key:'uia_fixed_pre_paste_preview_delay', label:'粘贴前预览(秒)', type:'number', ph:'1'},
      {key:'uia_fixed_pre_send_delay', label:'随机发送等待上限(秒，下限少2秒)', type:'number', ph:'5'},
      {key:'uia_fixed_settle_jitter_max_seconds', label:'点击与粘贴稳定随机间隔上限(秒)', type:'number', ph:'0.25'},
    ]},
    {title:'红包与转账接收', fields:[
      {key:'money_receive_enabled', label:'启用接收 Agent', type:'checkbox'},
      {key:'money_receive_timeout_seconds', label:'单次事务期限(秒)', type:'number', ph:'180'},
      {key:'money_receipt_poll_seconds', label:'WeFlow 回执轮询(秒)', type:'number', ph:'1'},
    ]},
    {title:'图片与视频描述', fields:[
      {key:'image_caption_provider', label:'描述服务', type:'select', opts:[{v:'ollama',l:'Ollama 本地'},{v:'openai',l:'OpenAI 兼容'}]},
      {key:'image_caption_model', label:'模型名', type:'text', ph:'qwen3.7-plus / llava:7b'},
      {key:'image_caption_api_key', label:'API Key', type:'password', ph:'sk-xxx (OpenAI模式时)'},
      {key:'image_caption_api_base', label:'API 地址', type:'text', ph:'https://dashscope.aliyuncs.com/compatible-mode/v1'},
      {key:'image_caption_prompt', label:'图片提示词', type:'textarea', ph:'请用中文描述图片...'},
      {key:'video_caption_prompt', label:'视频提示词', type:'textarea', ph:'请用中文描述视频...'},
      {key:'video_caption_max_mib', label:'视频上限(MiB)', type:'number', ph:'6'},
    ]},
    {title:'Ollama（使用本地模式时）', fields:[
      {key:'ollama_base_url', label:'Ollama 地址', type:'text', ph:'http://127.0.0.1:61000'},
      {key:'ollama_timeout', label:'超时(秒)', type:'number', ph:'60'},
    ]},
  ];

  groups.forEach(function(g){
    html += '<div class="settings-group"><h3>' + g.title + '</h3><div class="settings-row">';
    g.fields.forEach(function(f){
      var val = cfg[f.key] !== undefined ? cfg[f.key] : '';
      if (Array.isArray(val)) val = val.join(', ');
      var safeVal = escapeHtml(val);
      html += '<div class="settings-field"><label>' + f.label + '</label>';
      if (f.type === 'select') {
        html += '<select id="cfg_' + f.key + '">';
        f.opts.forEach(function(o){html += '<option value="' + o.v + '"' + (val==o.v?' selected':'') + '>' + o.l + '</option>'});
        html += '</select>';
      } else if (f.type === 'textarea') {
        html += '<textarea id="cfg_' + f.key + '" placeholder="' + escapeHtml(f.ph||'') + '" rows="2">' + safeVal + '</textarea>';
      } else if (f.type === 'number') {
        html += '<input type="number" id="cfg_' + f.key + '" value="' + safeVal + '" placeholder="' + escapeHtml(f.ph||'') + '">';
      } else if (f.type === 'checkbox') {
        html += '<input type="checkbox" id="cfg_' + f.key + '"' + (val ? ' checked' : '') + '>';
      } else {
        html += '<input type="' + f.type + '" id="cfg_' + f.key + '" value="' + safeVal + '" placeholder="' + escapeHtml(f.ph||'') + '">';
      }
      html += '</div>';
    });
    html += '</div></div>';
  });

  document.getElementById('settingsForm').innerHTML = html;
}

// ===== 保存配置 =====
function saveConfig() {
  // 从表单收集数据
  var fields = document.querySelectorAll('#settingsForm [id^="cfg_"]');
  var data = {};
  fields.forEach(function(el){
    var key = el.id.replace('cfg_','');
    var val = el.type === 'checkbox' ? el.checked : el.value.trim();
    if ((key === 'access_token' || key === 'image_caption_api_key') && !val) return;
    if (el.type === 'number') val = Number(val) || 0;
    // bot_nicknames: 逗号分隔转数组
    if (key === 'bot_nicknames') val = val ? val.split(/[,，]\\s*/).filter(Boolean) : [];
    data[key] = val;
  });

  fetch('/api/config', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(data),
  }).then(function(r){return r.json()}).then(function(res){
    if (res.ok) {
      showMsg('✅ 已保存（部分更改需重启生效）');
    } else {
      showMsg('❌ 保存失败');
    }
  }).catch(function(e){
    showMsg('❌ 保存失败: ' + e.message);
  });
}

// ===== 初始化 =====
refreshDashboard();
refreshChatHistory();
refreshDraftQuarantines();
refreshCommitUnknown();
refreshRecovery();
setInterval(refreshDashboard, 500);
setInterval(refreshChatHistory, 2000);
setInterval(refreshDraftQuarantines, 2000);
setInterval(refreshCommitUnknown, 2000);
setInterval(function(){
  if (document.getElementById('page-recovery').classList.contains('active')) {
    refreshRecovery();
  }
}, 3000);
</script>
</body>
</html>"""


def _is_calibrated() -> bool:
    try:
        validate_calibration(config.UIA_FIXED_CALIBRATION)
        return True
    except CalibrationError:
        return False


def _sender_status() -> dict[str, object]:
    return {"sender_mode": "uia_fixed", "calibrated": _is_calibrated()}


def _window_diagnostic_status() -> dict[str, object]:
    """Inspect the calibrated WeChat main window without changing UI state."""

    result: dict[str, object] = {
        "calibrated": _is_calibrated(),
        "found": False,
        "visible": False,
        "maximized": False,
        "foreground": False,
        "ready": False,
        "error_code": "",
    }
    if not result["calibrated"]:
        result["error_code"] = CALIBRATION_REQUIRED
        return result
    try:
        driver = Win32WeChatDriver()
        hwnd = driver.find_wechat_window()
        metrics = driver.get_client_metrics(hwnd)
        result.update(
            {
                "found": True,
                "visible": bool(metrics.visible),
                "maximized": bool(metrics.maximized),
                "foreground": bool(metrics.foreground),
                "client_width": int(metrics.width),
                "client_height": int(metrics.height),
                "dpi": int(metrics.dpi),
            }
        )
        validate_runtime_metrics(config.UIA_FIXED_CALIBRATION, metrics)
        result["ready"] = True
    except CalibrationError as error:
        result["error_code"] = str(getattr(error, "code", CALIBRATION_WINDOW))
    except Exception:
        log.exception("[Web] 微信窗口状态检测失败")
        result["error_code"] = CALIBRATION_WINDOW
    return result


def _recovery_store_snapshot() -> dict[str, object]:
    snapshot = default_store().recovery_snapshot()
    route_lookup = getattr(state, "get_private_route", None)
    if callable(route_lookup):
        for row in snapshot["targets"]:
            if row.get("routing_name"):
                continue
            try:
                route = route_lookup(row.get("target_id"))
            except Exception:
                route = None
            if isinstance(route, dict):
                row["routing_name"] = str(route.get("routing_name") or "")
    return snapshot


def _recovery_snapshot(*, inspect_window: bool = True) -> dict[str, object]:
    snapshot = _recovery_store_snapshot()

    capability_getter = getattr(state, "merged_reply_capability_status", None)
    capability = (
        capability_getter()
        if callable(capability_getter)
        else {
            "present": False,
            "valid": False,
            "ready": False,
            "generation": 0,
            "expires_in_seconds": 0.0,
            "phase": "disconnected",
        }
    )
    health_getter = getattr(state, "merged_reply_health", None)
    health = health_getter() if callable(health_getter) else snapshot["summary"]
    return {
        **snapshot,
        "window": (
            _window_diagnostic_status()
            if inspect_window
            else {"ready": False, "error_code": "E_NOT_INSPECTED"}
        ),
        "capability": capability,
        "health": health,
        "last_send_result": state.get_last_send_result(),
        "running": bool(state.running),
        "paused": bool(state.paused.is_set()),
    }


def _money_service():
    lock = getattr(state, "bridge_lock", None)
    if lock is None:
        bridge = getattr(state, "bridge_instance", None)
        return getattr(bridge, "money_actions", None) if bridge is not None else None
    with lock:
        bridge = getattr(state, "bridge_instance", None)
        return getattr(bridge, "money_actions", None) if bridge is not None else None


def _money_request(handler, request):
    query = parse_qs(request.query, keep_blank_values=True)
    if set(query) != {"request_id"} or len(query["request_id"]) != 1:
        raise MoneyRequestError("E_MONEY_REQUEST")
    authorization = handler.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise MoneyRequestError("E_MONEY_CAPABILITY", 403)
    service = _money_service()
    if service is None:
        raise MoneyRequestError("E_MONEY_UNAVAILABLE", 409)
    return service, query["request_id"][0], authorization[7:]


_PRIVATE_CONFIG_KEYS = {
    "uia_fixed_calibration",
    "access_token",
    "image_caption_api_key",
}
_SECRET_CONFIG_KEYS = {"access_token", "image_caption_api_key"}


def _public_config(value: dict[str, object]) -> dict[str, object]:
    public = {
        key: field_value
        for key, field_value in value.items()
        if key not in _PRIVATE_CONFIG_KEYS
    }
    public.setdefault(
        "uia_fixed_pre_paste_preview_delay",
        config.UIA_FIXED_PRE_PASTE_PREVIEW_DELAY,
    )
    public.setdefault(
        "uia_fixed_pre_send_delay",
        config.UIA_FIXED_PRE_SEND_DELAY,
    )
    public.setdefault(
        "uia_fixed_settle_jitter_max_seconds",
        config.UIA_FIXED_SETTLE_JITTER_MAX_SECONDS,
    )
    public.setdefault(
        "buffer_quiet_seconds",
        getattr(config, "BUFFER_QUIET_SECONDS", 1.5),
    )
    public.setdefault(
        "buffer_max_seconds",
        getattr(config, "BUFFER_MAX_SECONDS", 5.0),
    )
    public.setdefault(
        "video_caption_prompt",
        getattr(
            config,
            "VIDEO_CAPTION_PROMPT",
            "请用中文简洁描述这段视频发生了什么，包括关键人物、动作、场景和屏幕文字",
        ),
    )
    public.setdefault(
        "money_receive_enabled",
        getattr(config, "MONEY_RECEIVE_ENABLED", True),
    )
    public.setdefault(
        "money_receive_timeout_seconds",
        getattr(config, "MONEY_RECEIVE_TIMEOUT_SECONDS", 180.0),
    )
    public.setdefault(
        "money_receipt_poll_seconds",
        getattr(config, "MONEY_RECEIPT_POLL_SECONDS", 1.0),
    )
    raw_video_limit = public.get("video_caption_max_mib", 6)
    try:
        normalized_video_limit = (
            6 if isinstance(raw_video_limit, bool) else int(raw_video_limit)
        )
    except (TypeError, ValueError):
        normalized_video_limit = 6
    if not 1 <= normalized_video_limit <= 6:
        normalized_video_limit = 6
    public["video_caption_max_mib"] = normalized_video_limit
    return public


def _merge_public_config(
    current: dict[str, object], submitted: dict[str, object]
) -> dict[str, object]:
    for key, field_value in submitted.items():
        if key == "uia_fixed_calibration":
            continue
        if key in {
            "uia_fixed_pre_paste_preview_delay",
            "uia_fixed_pre_send_delay",
            "uia_fixed_settle_jitter_max_seconds",
        }:
            if isinstance(field_value, bool) or not isinstance(
                field_value, (int, float)
            ):
                raise ValueError("invalid UIA review delay")
            maximum = {
                "uia_fixed_pre_paste_preview_delay": 10.0,
                "uia_fixed_pre_send_delay": 60.0,
                "uia_fixed_settle_jitter_max_seconds": 0.5,
            }[key]
            if (
                not math.isfinite(float(field_value))
                or not 0.0 <= float(field_value) <= maximum
            ):
                raise ValueError("invalid UIA review delay")
            field_value = float(field_value)
        if key in {"buffer_quiet_seconds", "buffer_max_seconds"}:
            if isinstance(field_value, bool) or not isinstance(
                field_value,
                (int, float),
            ):
                raise ValueError("invalid buffer timing")
            minimum, maximum = (
                (0.2, 10.0)
                if key == "buffer_quiet_seconds"
                else (0.2, 30.0)
            )
            if (
                not math.isfinite(float(field_value))
                or not minimum <= float(field_value) <= maximum
            ):
                raise ValueError("invalid buffer timing")
            field_value = float(field_value)
        if key == "video_caption_max_mib":
            if (
                isinstance(field_value, bool)
                or not isinstance(field_value, (int, float))
                or not float(field_value).is_integer()
                or not 1 <= int(field_value) <= 6
            ):
                raise ValueError("invalid video caption size")
            field_value = int(field_value)
        if key == "money_receive_enabled":
            if not isinstance(field_value, bool):
                raise ValueError("invalid money receive flag")
        if key in {
            "money_receive_timeout_seconds",
            "money_receipt_poll_seconds",
        }:
            if isinstance(field_value, bool) or not isinstance(
                field_value,
                (int, float),
            ):
                raise ValueError("invalid money receive timing")
            minimum, maximum = (
                (30.0, 600.0)
                if key == "money_receive_timeout_seconds"
                else (0.2, 5.0)
            )
            if (
                not math.isfinite(float(field_value))
                or not minimum <= float(field_value) <= maximum
            ):
                raise ValueError("invalid money receive timing")
            field_value = float(field_value)
        if key in _SECRET_CONFIG_KEYS:
            if not isinstance(field_value, str) or not field_value.strip():
                continue
        current[key] = field_value
    resolver = getattr(config, "resolve_buffer_windows", None)
    if callable(resolver):
        quiet, maximum, rejected_pair = resolver(current)
        if rejected_pair:
            raise ValueError("buffer max must not be shorter than quiet window")
    else:
        quiet = float(current.get("buffer_quiet_seconds", 1.5))
        maximum = float(
            current.get("buffer_max_seconds", current.get("buffer_seconds", 5.0))
        )
        if maximum < quiet:
            raise ValueError("buffer max must not be shorter than quiet window")
    current["buffer_quiet_seconds"] = quiet
    current["buffer_max_seconds"] = maximum
    current["buffer_seconds"] = maximum
    return current


class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not _request_is_local(self):
            self.send_json({"error": "E_LOCAL_ONLY"}, 403)
            return
        request = urlsplit(self.path)
        if request.path == "/status":
            ob_connected = state._ob_ws is not None and state._ob_ws_ready.is_set()
            weflow_connected = state.bridge_instance is not None and state.bridge_instance._sse_session is not None
            money_service = _money_service()
            status = {
                "running": state.running,
                "paused": state.paused.is_set(),
                "ob_connected": ob_connected,
                "weflow_connected": weflow_connected,
                "group_reply_mode": state.group_reply_mode,
                "send_preview": state.get_send_preview(),
                "last_send_result": state.get_last_send_result(),
                "money_receive": (
                    money_service.public_status()
                    if money_service is not None
                    else {"active": False}
                ),
            }
            merged_health = getattr(state, "merged_reply_health", None)
            if callable(merged_health):
                status.update(merged_health())
            status.update(_sender_status())
            self.send_json(status)
        elif request.path == "/api/recovery":
            try:
                self.send_json(_recovery_snapshot())
            except Exception:
                log.exception("[Web] 故障恢复状态读取失败")
                self.send_json({"error": "E_RECOVERY_READ"}, 500)
        elif request.path == "/draft-quarantines":
            try:
                self.send_json(
                    {"quarantines": default_store().list_quarantines()}
                )
            except Exception:
                self.send_json({"error": "E_RECOVERY_READ"}, 500)
        elif request.path == "/commit-unknown":
            try:
                self.send_json({"rows": default_store().list_commit_unknown()})
            except Exception:
                self.send_json({"error": "E_RECOVERY_READ"}, 500)
        elif request.path in {
            "/api/money-action/frame",
            "/api/money-action/status",
        }:
            try:
                service, request_id, token = _money_request(self, request)
                if request.path.endswith("/frame"):
                    payload = service.get_frame(
                        request_id=request_id,
                        token=token,
                    )
                else:
                    payload = service.get_status(
                        request_id=request_id,
                        token=token,
                    )
                self.send_json(payload)
            except MoneyRequestError as error:
                self.send_json(
                    {"error": error.code},
                    error.http_status,
                )
            except Exception:
                self.send_json({"error": "E_MONEY_REQUEST"}, 500)
        elif request.path == "/api/chat-history":
            try:
                query = parse_qs(request.query, keep_blank_values=True)
                if set(query) - {"limit"}:
                    raise ValueError("unsupported chat history parameter")
                submitted_limit = query.get(
                    "limit", [str(_CHAT_HISTORY_DEFAULT_LIMIT)]
                )
                if (
                    len(submitted_limit) != 1
                    or not submitted_limit[0].isdigit()
                ):
                    raise ValueError("invalid chat history limit")
                limit = int(submitted_limit[0])
                records = _read_chat_records(config.BRIDGE_LOG_FILE, limit)
                self.send_json({"records": records})
            except ValueError:
                self.send_json({"error": "E_CHAT_HISTORY_REQUEST"}, 400)
            except OSError:
                self.send_json({"error": "E_CHAT_HISTORY_READ"}, 500)
        elif request.path == "/api/config":
            try:
                with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self.send_json(_public_config(cfg))
            except Exception:
                self.send_json({"error": "E_CONFIG_READ"}, 500)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(PAGE.encode("utf-8"))

    def do_POST(self):
        if not _request_is_local(self, write=True):
            self.send_json({"error": "E_LOCAL_ONLY"}, 403)
            return
        request_path = urlsplit(self.path).path
        if request_path == "/api/money-action/step":
            try:
                request = urlsplit(self.path)
                service, request_id, token = _money_request(self, request)
                payload = self.read_json_body(16384)
                if str(payload.get("request_id") or "") != request_id:
                    raise MoneyRequestError("E_MONEY_REQUEST")
                result = service.submit_step(token=token, payload=payload)
                self.send_json(result)
            except MoneyRequestError as error:
                self.send_json(
                    {"error": error.code},
                    error.http_status,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                self.send_json({"error": "E_MONEY_ACTION"}, 400)
            except Exception:
                self.send_json({"error": "E_MONEY_ACTION"}, 500)
        elif request_path == "/start":
            from main import _start_bridge
            _start_bridge()
            self.send_json({"ok": True})
        elif request_path == "/stop":
            from main import _stop_bridge
            try:
                _stop_bridge()
                self.send_json({"ok": True})
            except RuntimeError as error:
                code = str(error)
                if not code.startswith("E_MERGED_REPLY_STOP_"):
                    code = "E_BRIDGE_STOP"
                self.send_json({"ok": False, "error": code}, 409)
        elif request_path == "/pause":
            state.paused.set()
            log.info("[Web] 已暂停")
            self.send_json({"ok": True})
        elif request_path == "/resume":
            state.paused.clear()
            log.info("[Web] 已恢复")
            self.send_json({"ok": True})
        elif request_path == "/api/recovery/rehandshake":
            try:
                payload = self.read_json_body(1024)
                if not isinstance(payload, dict) or payload:
                    raise ValueError("unexpected handshake payload")
                restart = getattr(state, "request_merged_reply_rehandshake", None)
                if not callable(restart):
                    raise ReplyStoreError("E_RECOVERY_MERGED_OFFLINE")
                self.send_json({"ok": True, **restart()})
            except ReplyStoreError as error:
                self.send_json({"ok": False, "error": error.code}, 409)
            except (TypeError, ValueError, json.JSONDecodeError):
                self.send_json(
                    {"ok": False, "error": "E_RECOVERY_REQUEST"}, 400
                )
        elif request_path == "/api/recovery/dismiss-result":
            try:
                payload = self.read_json_body(1024)
                if not isinstance(payload, dict) or payload:
                    raise ValueError("unexpected dismiss payload")
                clear_result = getattr(state, "clear_last_send_result", None)
                if callable(clear_result):
                    clear_result()
                self.send_json({"ok": True})
            except (TypeError, ValueError, json.JSONDecodeError):
                self.send_json(
                    {"ok": False, "error": "E_RECOVERY_REQUEST"}, 400
                )
        elif request_path == "/api/recovery/contact":
            try:
                payload = self.read_json_body(2048)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"target_id"}
                    or isinstance(payload["target_id"], bool)
                    or not isinstance(payload["target_id"], int)
                    or payload["target_id"] <= 0
                ):
                    raise ValueError("invalid recovery target")
                if state.running and not state.paused.is_set():
                    raise ReplyStoreError("E_RECOVERY_PAUSE_REQUIRED")
                if state.get_send_preview() is not None:
                    raise ReplyStoreError("E_RECOVERY_JOB_BUSY")
                result = default_store().release_stale_target(
                    payload["target_id"]
                )
                self.send_json({"ok": True, **result})
            except ReplyStoreError as error:
                self.send_json({"ok": False, "error": error.code}, 409)
            except (TypeError, ValueError, json.JSONDecodeError):
                self.send_json(
                    {"ok": False, "error": "E_RECOVERY_REQUEST"}, 400
                )
        elif request_path == "/cancel-current":
            try:
                payload = self.read_json_body(1024)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"preview_id"}
                    or isinstance(payload["preview_id"], bool)
                    or not isinstance(payload["preview_id"], int)
                    or payload["preview_id"] <= 0
                ):
                    raise ValueError("invalid preview id")
                cancelled = state.cancel_current_preview(payload["preview_id"])
                self.send_json({"ok": True, "cancelled": cancelled})
            except Exception:
                self.send_json({"ok": False, "error": "E_CANCEL_REQUEST"}, 400)
        elif request_path == "/resolve-draft-quarantine":
            try:
                payload = self.read_json_body(4096)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"target_id", "request_id"}
                    or isinstance(payload["target_id"], bool)
                    or not isinstance(payload["target_id"], int)
                    or payload["target_id"] <= 0
                    or not isinstance(payload["request_id"], str)
                    or not payload["request_id"].strip()
                ):
                    raise ValueError("invalid quarantine identity")
                resolved = state.clear_reply_draft_quarantine(
                    payload["target_id"],
                    payload["request_id"],
                )
                self.send_json({"ok": True, "resolved": resolved})
            except (TypeError, ValueError, json.JSONDecodeError):
                self.send_json(
                    {"ok": False, "error": "E_DRAFT_QUARANTINE_REQUEST"},
                    400,
                )
        elif request_path == "/resolve-commit-unknown":
            try:
                payload = self.read_json_body(4096)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"request_id", "revision", "resolution"}
                    or not isinstance(payload["request_id"], str)
                    or not payload["request_id"].strip()
                    or isinstance(payload["revision"], bool)
                    or not isinstance(payload["revision"], int)
                    or payload["revision"] <= 0
                    or payload["resolution"] not in {"sent", "not_sent"}
                ):
                    raise ValueError("invalid commit resolution")
                resolved = default_store().resolve_commit_unknown(
                    payload["request_id"],
                    payload["revision"],
                    payload["resolution"],
                )
                self.send_json({"ok": True, "resolved": resolved})
            except (TypeError, ValueError, json.JSONDecodeError):
                self.send_json(
                    {"ok": False, "error": "E_COMMIT_UNKNOWN_REQUEST"},
                    400,
                )
        elif request_path == "/mode":
            mode_order = ["mention", "all", "batch"]
            idx = mode_order.index(state.group_reply_mode) if state.group_reply_mode in mode_order else -1
            new_mode = mode_order[(idx + 1) % len(mode_order)]
            state.group_reply_mode = new_mode
            try:
                with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg["group_reply_mode"] = new_mode
                with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=4)
                    f.write("\n")
                log.info(f"[Web] 群聊模式已切换为: {new_mode}")
            except Exception:
                log.error("[Web] 保存配置失败")
            self.send_json({"ok": True, "group_reply_mode": new_mode})
        elif request_path == "/api/config":
            try:
                new_cfg = self.read_json_body(65536)

                # 读取当前配置，仅覆盖前端传来的字段
                with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
                    current = json.load(f)
                _merge_public_config(current, new_cfg)
                # 保留 _comment 字段
                if "_comment" not in current:
                    current["_comment"] = "微信 ↔ AstrBot 桥接 - OneBot v11 版配置"

                with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(current, f, ensure_ascii=False, indent=4)
                    f.write("\n")

                log.info(f"[Web] 配置已保存")
                # 运行时同步 group_reply_mode
                if "group_reply_mode" in new_cfg:
                    state.group_reply_mode = new_cfg["group_reply_mode"]
                if "buffer_quiet_seconds" in new_cfg:
                    config.BUFFER_QUIET_SECONDS = float(
                        new_cfg["buffer_quiet_seconds"]
                    )
                if "buffer_max_seconds" in new_cfg:
                    config.BUFFER_MAX_SECONDS = float(
                        new_cfg["buffer_max_seconds"]
                    )
                    config.BUFFER_SECONDS = config.BUFFER_MAX_SECONDS
                if "uia_fixed_pre_paste_preview_delay" in new_cfg:
                    delay = float(new_cfg["uia_fixed_pre_paste_preview_delay"])
                    config.UIA_FIXED_PRE_PASTE_PREVIEW_DELAY = delay
                    sender = state.sender_instance
                    if sender is not None:
                        sender.pre_paste_preview_delay = delay
                if "uia_fixed_pre_send_delay" in new_cfg:
                    delay = float(new_cfg["uia_fixed_pre_send_delay"])
                    config.UIA_FIXED_PRE_SEND_DELAY = delay
                    sender = state.sender_instance
                    if sender is not None:
                        sender.pre_send_delay = delay
                if "uia_fixed_settle_jitter_max_seconds" in new_cfg:
                    delay = float(
                        new_cfg["uia_fixed_settle_jitter_max_seconds"]
                    )
                    config.UIA_FIXED_SETTLE_JITTER_MAX_SECONDS = delay
                    sender = state.sender_instance
                    if sender is not None:
                        sender.settle_jitter_max_seconds = delay

                self.send_json({"ok": True})
            except Exception:
                log.error("[Web] 保存配置异常")
                self.send_json({"ok": False, "error": "E_CONFIG_SAVE"}, 500)
        else:
            self.send_json({"ok": False}, 404)

    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def read_json_body(self, maximum_bytes):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > maximum_bytes:
            raise ValueError("invalid request size")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def log_message(self, fmt, *args):
        pass
