"""
Web 控制面板模块。

提供可视化控制页面（http://127.0.0.1:WEB_PORT），
支持启停/暂停/恢复桥接，显示运行状态和日志，
以及在线编辑 config.json 配置。
"""

import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

import state
import config
from uia_support import CalibrationError, validate_calibration

log = logging.getLogger("ob11-bridge")


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

/* ===== 日志 ===== */
.log-box{flex:1;min-height:100px;background:#fff;border:1px solid var(--line);border-radius:6px;padding:12px;font-size:12px;font-family:'Cascadia Code','Fira Code',monospace;color:#374151;overflow-y:auto;line-height:1.6;white-space:pre-wrap}
.log-box:empty::before{content:'等待连接...';color:#9ca3af}
.log-box::-webkit-scrollbar{width:4px}
.log-box::-webkit-scrollbar-thumb{background:#d1d5db;border-radius:4px}

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

    <div class="btn-row">
      <button class="btn btn-pink" id="btnStart" onclick="action('start')">启动</button>
      <button class="btn btn-red" id="btnStop" onclick="action('stop')" disabled>停止</button>
      <button class="btn btn-amber" id="btnPause" onclick="action('pause')" disabled>暂停</button>
      <button class="btn btn-green" id="btnResume" onclick="action('resume')" style="display:none" disabled>恢复</button>
    </div>

    <div class="mode-row">
      <span>群聊模式:</span>
      <span class="mode-value" id="modeStatus">-</span>
      <button class="btn btn-outline" id="btnToggleMode" style="padding:5px 14px;font-size:12px">切换</button>
    </div>

    <div class="log-box" id="log">等待连接...</div>
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

// ===== Tab 切换 =====
function switchTab(name) {
  document.querySelectorAll('.tab-page').forEach(function(p){p.classList.remove('active')});
  document.getElementById('page-' + name).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active')});
  document.querySelector('[data-tab="' + name + '"]').classList.add('active');
  if (name === 'settings') loadConfig();
}

// ===== 面板刷新 =====
var modeMap = {'mention':'仅@回复','all':'全部回复','batch':'批处理'};

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
    document.getElementById('sendMethod').textContent = s.sender_mode + (s.calibrated ? '（已标定）' : '（待标定）');

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

    var logEl = document.getElementById('log');
    var isAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    logEl.textContent = s.log || '';
    if (isAtBottom) logEl.scrollTop = logEl.scrollHeight;
  });
}

function action(cmd) {
  fetch('/' + cmd, {method:'POST'}).then(function(){setTimeout(refreshDashboard,500)});
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
      {key:'buffer_seconds', label:'消息缓冲(秒)', type:'number', ph:'5'},
      {key:'group_reply_mode', label:'群聊回复模式', type:'select', opts:[{v:'mention',l:'仅@回复'},{v:'all',l:'全部回复'},{v:'batch',l:'批处理'}]},
      {key:'web_port', label:'Web 面板端口', type:'number', ph:'8766'},
    ]},
    {title:'图片描述', fields:[
      {key:'image_caption_provider', label:'描述服务', type:'select', opts:[{v:'ollama',l:'Ollama 本地'},{v:'openai',l:'OpenAI 兼容'}]},
      {key:'image_caption_model', label:'模型名', type:'text', ph:'kimi-k2.6 / llava:7b'},
      {key:'image_caption_api_key', label:'API Key', type:'password', ph:'sk-xxx (OpenAI模式时)'},
      {key:'image_caption_api_base', label:'API 地址', type:'text', ph:'https://api.moonshot.cn/v1'},
      {key:'image_caption_prompt', label:'描述提示词', type:'textarea', ph:'请用中文描述...'},
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
      html += '<div class="settings-field"><label>' + f.label + '</label>';
      if (f.type === 'select') {
        html += '<select id="cfg_' + f.key + '">';
        f.opts.forEach(function(o){html += '<option value="' + o.v + '"' + (val==o.v?' selected':'') + '>' + o.l + '</option>'});
        html += '</select>';
      } else if (f.type === 'textarea') {
        html += '<textarea id="cfg_' + f.key + '" placeholder="' + (f.ph||'') + '" rows="2">' + val + '</textarea>';
      } else if (f.type === 'number') {
        html += '<input type="number" id="cfg_' + f.key + '" value="' + val + '" placeholder="' + (f.ph||'') + '">';
      } else {
        html += '<input type="' + f.type + '" id="cfg_' + f.key + '" value="' + val.replace(/"/g,'&quot;') + '" placeholder="' + (f.ph||'') + '">';
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
    var val = el.value.trim();
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
setInterval(refreshDashboard, 3000);
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


_PRIVATE_CONFIG_KEYS = {
    "uia_fixed_calibration",
    "access_token",
    "image_caption_api_key",
}
_SECRET_CONFIG_KEYS = {"access_token", "image_caption_api_key"}


def _public_config(value: dict[str, object]) -> dict[str, object]:
    return {
        key: field_value
        for key, field_value in value.items()
        if key not in _PRIVATE_CONFIG_KEYS
    }


def _merge_public_config(
    current: dict[str, object], submitted: dict[str, object]
) -> dict[str, object]:
    for key, field_value in submitted.items():
        if key == "uia_fixed_calibration":
            continue
        if key in _SECRET_CONFIG_KEYS:
            if not isinstance(field_value, str) or not field_value.strip():
                continue
        current[key] = field_value
    return current


class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            ob_connected = state._ob_ws is not None and state._ob_ws_ready.is_set()
            weflow_connected = state.bridge_instance is not None and state.bridge_instance._sse_session is not None
            status = {
                "running": state.running,
                "paused": state.paused.is_set(),
                "ob_connected": ob_connected,
                "weflow_connected": weflow_connected,
                "group_reply_mode": state.group_reply_mode,
                "log": (
                    "bridge.log 已记录完整联系人和聊天正文。"
                    "出于安全考虑，不在网页面板显示；"
                    "请仅在本机 data\\logs\\bridge.log 中查看。"
                ),
            }
            status.update(_sender_status())
            self.send_json(status)
        elif self.path == "/api/config":
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
        if self.path == "/start":
            from main import _start_bridge
            _start_bridge()
            self.send_json({"ok": True})
        elif self.path == "/stop":
            from main import _stop_bridge
            _stop_bridge()
            self.send_json({"ok": True})
        elif self.path == "/pause":
            state.paused.set()
            log.info("[Web] 已暂停")
            self.send_json({"ok": True})
        elif self.path == "/resume":
            state.paused.clear()
            log.info("[Web] 已恢复")
            self.send_json({"ok": True})
        elif self.path == "/mode":
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
        elif self.path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                new_cfg = json.loads(body)

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

                self.send_json({"ok": True})
            except Exception:
                log.error("[Web] 保存配置异常")
                self.send_json({"ok": False, "error": "E_CONFIG_SAVE"}, 500)
        else:
            self.send_json({"ok": False}, 404)

    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):
        pass
