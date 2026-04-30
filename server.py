"""
THEDARKGEEKDC — P2P File Share
Railway-ready | Zero Upload | WebRTC Direct | Multi-User
Developer: @thedarkgeekdc (Mr DK CHAUDHARY)
"""

import os
import io
import time
import random
import socket
import threading

# FIX #1: eventlet monkey-patch MUST be first — before any other imports
# Without this, eventlet can't properly handle Python's threading/socket/time
import eventlet
eventlet.monkey_patch()

import qrcode
from flask import Flask, request, send_file, Response, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room

# ─────────────────────────────────────────────────────────────────
#  APP SETUP
# ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "p2p_thedarkgeekdc_2025")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=20,
    max_http_buffer_size=1024 * 1024,  # 1MB max signaling message
)

PORT = int(os.environ.get("PORT", 5683))


# ─────────────────────────────────────────────────────────────────
#  NETWORK HELPERS
# ─────────────────────────────────────────────────────────────────
def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ─────────────────────────────────────────────────────────────────
#  ROOM MANAGEMENT
# ─────────────────────────────────────────────────────────────────
rooms: dict = {}
rooms_lock = threading.Lock()


def _cleanup_loop():
    """Expire rooms older than 2 hours."""
    while True:
        eventlet.sleep(300)
        cutoff = time.time() - 7200
        with rooms_lock:
            expired = [c for c, r in rooms.items() if r["created_at"] < cutoff]
            for c in expired:
                del rooms[c]
                print(f"[Cleanup] Room {c} expired")


eventlet.spawn(_cleanup_loop)  # Use eventlet.spawn instead of threading.Thread


# ─────────────────────────────────────────────────────────────────
#  SOCKET EVENTS  (signaling only — zero file data)
# ─────────────────────────────────────────────────────────────────

@socketio.on("create_room")
def on_create_room():
    sid = request.sid
    with rooms_lock:
        # Find an unused 4-digit code
        for _ in range(300):
            code = str(random.randint(1000, 9999))
            if code not in rooms:
                break
        else:
            emit("error", {"msg": "Server is busy. Please try again later."})
            return

        rooms[code] = {
            "sender": sid,
            "receivers": set(),
            "created_at": time.time(),
        }

    join_room(code)
    emit("room_created", {"code": code})
    print(f"[+] Room {code} created by {sid[:8]}")


@socketio.on("join_room_request")
def on_join_room(data):
    code = str(data.get("code", "")).strip()
    sid  = request.sid

    with rooms_lock:
        if code not in rooms:
            emit("error", {"msg": "Invalid code or room has expired."})
            return
        room        = rooms[code]
        sender_sid  = room["sender"]
        room["receivers"].add(sid)

    join_room(code)
    # Notify sender a new receiver joined
    socketio.emit("receiver_joined", {"receiver_sid": sid}, to=sender_sid)
    emit("joined", {"code": code, "sender_sid": sender_sid})
    print(f"[+] {sid[:8]} joined room {code}")


@socketio.on("webrtc_offer")
def on_offer(data):
    to = data.get("to")
    if to:
        socketio.emit("webrtc_offer", {"offer": data["offer"], "from": request.sid}, to=to)


@socketio.on("webrtc_answer")
def on_answer(data):
    to = data.get("to")
    if to:
        socketio.emit("webrtc_answer", {"answer": data["answer"], "from": request.sid}, to=to)


@socketio.on("webrtc_ice")
def on_ice(data):
    to = data.get("to")
    if to:
        socketio.emit(
            "webrtc_ice",
            {"candidate": data["candidate"], "from": request.sid},
            to=to,
        )


# FIX #2: New event — sender signals transfer stopped to all receivers
@socketio.on("transfer_stop")
def on_transfer_stop(data):
    """Sender broadcast: stop transfer for one or all receivers."""
    sid  = request.sid
    code = data.get("code")
    to   = data.get("to")   # optional: specific receiver sid
    if to:
        socketio.emit("transfer_stopped", {"from": sid}, to=to)
    elif code:
        socketio.emit("transfer_stopped", {"from": sid}, to=code, include_self=False)


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    with rooms_lock:
        for code, room in list(rooms.items()):
            if sid == room["sender"]:
                # FIX #3: Notify all receivers in the room, then clean up room
                socketio.emit("sender_left", {}, to=code)
                # Remove all receivers from room tracking
                for rsid in list(room["receivers"]):
                    leave_room(code, sid=rsid)
                del rooms[code]
                print(f"[-] Sender left → room {code} closed")
                break
            elif sid in room["receivers"]:
                room["receivers"].discard(sid)
                # FIX #4: Leave the socket.io room so future broadcasts skip this sid
                leave_room(code)
                socketio.emit("receiver_left", {"sid": sid}, to=room["sender"])
                print(f"[-] Receiver {sid[:8]} left room {code}")
                break


# ─────────────────────────────────────────────────────────────────
#  HTTP ROUTES
# ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")


@app.route("/api/localip")
def api_localip():
    """Returns the server machine's local WiFi IP and port."""
    return jsonify({"ip": get_local_ip(), "port": PORT})


@app.route("/qr")
def gen_qr():
    data = request.args.get("d", request.host_url)
    size = int(request.args.get("s", 8))
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=size,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#00d4ff", back_color="#050510")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")



# ─────────────────────────────────────────────────────────────────
#  HTML + CSS + JS  (single-file, no external HTML files)
# ─────────────────────────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ShareAnywhere — Send Files Instantly</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#07071a;--surface:#0f0f2e;--card:#13133a;--border:#252560;
  --cyan:#00c8f0;--purple:#a066ff;--green:#00d98a;--red:#ff4d6d;--amber:#ffbb33;
  --text:#eeeeff;--sub:#8888bb;
}
html{height:100%}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
  background-image:radial-gradient(ellipse 70% 50% at 20% 0%,rgba(0,200,240,.06),transparent),radial-gradient(ellipse 60% 40% at 80% 100%,rgba(160,102,255,.06),transparent);}
img{max-width:100%;display:block}button{font-family:inherit;cursor:pointer}input{font-family:inherit}

.wrap{max-width:500px;margin:0 auto;padding:0 16px}
.page{min-height:100vh;display:flex;flex-direction:column}
.hidden{display:none!important}

/* Header */
.hdr{padding:16px 0;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border);margin-bottom:0}
.hdr-logo{font-size:1.1rem;font-weight:900;letter-spacing:1px;
  background:linear-gradient(90deg,var(--cyan),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hdr-back{background:none;border:none;color:var(--sub);padding:6px;border-radius:8px;
  font-size:.85rem;font-weight:600;display:flex;align-items:center;gap:4px;transition:color .2s}
.hdr-back:hover{color:var(--text)}
.hdr-spacer{flex:1}
.dot{width:8px;height:8px;border-radius:50%;background:var(--sub);transition:.4s;flex-shrink:0}
.dot.on{background:var(--green);box-shadow:0 0 8px var(--green)}

/* Home */
#home{text-align:center}
.home-hero{padding:44px 0 36px}
.home-icon{font-size:3.8rem;margin-bottom:18px;line-height:1}
.home-title{font-size:2rem;font-weight:900;line-height:1.15;margin-bottom:10px}
.home-title .hl{background:linear-gradient(90deg,var(--cyan),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.home-sub{font-size:.9rem;color:var(--sub);line-height:1.6;margin-bottom:28px}

.privacy-badge{display:inline-flex;align-items:center;gap:8px;
  background:rgba(0,217,138,.08);border:1px solid rgba(0,217,138,.2);
  border-radius:40px;padding:8px 18px;margin-bottom:36px;
  font-size:.78rem;color:var(--green);font-weight:600}

.mode-btns{display:flex;flex-direction:column;gap:12px;margin-bottom:36px}
.mode-btn{display:flex;align-items:center;gap:14px;background:var(--card);border:1.5px solid var(--border);
  border-radius:16px;padding:18px 20px;text-align:left;transition:all .22s;width:100%}
.mode-btn:hover{transform:translateY(-2px)}
.mode-btn.wifi:hover{border-color:var(--cyan);box-shadow:0 0 28px rgba(0,200,240,.15)}
.mode-btn.inet:hover{border-color:var(--purple);box-shadow:0 0 28px rgba(160,102,255,.15)}
.mbi{width:50px;height:50px;border-radius:13px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:1.5rem}
.wifi .mbi{background:rgba(0,200,240,.12)}
.inet .mbi{background:rgba(160,102,255,.12)}
.mbt{flex:1;min-width:0}
.mbt-title{font-size:.95rem;font-weight:800;margin-bottom:3px}
.wifi .mbt-title{color:var(--cyan)}
.inet .mbt-title{color:var(--purple)}
.mbt-desc{font-size:.76rem;color:var(--sub);line-height:1.4}
.mba{color:var(--sub);font-size:1rem;flex-shrink:0}

.spec-row{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-bottom:40px}
.spec{font-size:.7rem;font-weight:700;color:var(--sub);background:var(--card);border:1px solid var(--border);border-radius:20px;padding:5px 13px}

/* Footer credit */
.footer{text-align:center;padding:20px 0 28px;margin-top:auto}
.footer-text{font-size:.72rem;color:var(--sub);line-height:1.7}
.footer-text a{color:var(--sub);text-decoration:none}
.footer-text strong{color:var(--cyan)}
.footer-divider{color:var(--border);margin:0 6px}

/* Screen layout */
.screen{padding:22px 0;display:flex;flex-direction:column}

/* Steps bar */
.steps{display:flex;align-items:center;justify-content:center;gap:6px;margin-bottom:24px}
.step{width:26px;height:4px;border-radius:4px;background:var(--border);transition:background .3s}
.step.done{background:var(--cyan)}
.step.half{background:var(--cyan);opacity:.4}

.sec-title{font-size:1.05rem;font-weight:800;margin-bottom:5px}
.sec-sub{font-size:.8rem;color:var(--sub);margin-bottom:18px;line-height:1.5}

/* Drop zone */
.dz{border:2px dashed var(--border);border-radius:14px;padding:38px 20px;text-align:center;cursor:pointer;
  transition:all .2s;position:relative;overflow:hidden;background:var(--card)}
.dz.over,.dz:hover{border-color:var(--cyan);background:rgba(0,200,240,.04)}
.dz input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.dz-icon{font-size:2.4rem;margin-bottom:9px}
.dz-msg{font-size:.92rem;font-weight:700;margin-bottom:4px}
.dz-hint{font-size:.75rem;color:var(--sub)}

/* File list */
.file-list{display:flex;flex-direction:column;gap:7px;margin-top:12px}
.file-item{display:flex;align-items:center;gap:11px;background:var(--card);border:1px solid var(--border);border-radius:11px;padding:10px 13px}
.file-ico{font-size:1.4rem;flex-shrink:0}
.file-info{flex:1;min-width:0}
.file-name{font-size:.83rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file-size{font-size:.7rem;color:var(--sub);margin-top:2px}
.file-del{background:none;border:none;color:var(--sub);padding:5px;border-radius:7px;font-size:.85rem;transition:color .2s;line-height:1;flex-shrink:0}
.file-del:hover{color:var(--red)}

.file-summary{display:flex;align-items:center;justify-content:space-between;padding:10px 2px;font-size:.78rem;color:var(--sub)}
.file-summary strong{color:var(--text)}

/* Buttons */
.big-btn{width:100%;padding:15px;border:none;border-radius:13px;font-size:.95rem;font-weight:800;cursor:pointer;transition:all .22s;display:flex;align-items:center;justify-content:center;gap:7px}
.big-btn:disabled{opacity:.35;cursor:not-allowed;transform:none!important}
.big-btn.cyan{background:linear-gradient(135deg,var(--cyan),#0099cc);color:#001a22}
.big-btn.cyan:not(:disabled):hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,200,240,.3)}
.big-btn.purple{background:linear-gradient(135deg,var(--purple),#7744cc);color:#fff}
.big-btn.purple:not(:disabled):hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(160,102,255,.3)}
.big-btn.ghost{background:var(--card);color:var(--sub);border:1px solid var(--border)}
.big-btn.ghost:hover{color:var(--text);border-color:var(--sub)}

/* Code card */
.code-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:26px 22px;margin-bottom:12px;text-align:center}
.code-label{font-size:.68rem;font-weight:700;letter-spacing:2px;color:var(--sub);margin-bottom:10px}
.code-digits{font-family:'Courier New',monospace;font-size:3.6rem;font-weight:900;letter-spacing:16px;
  color:var(--cyan);text-shadow:0 0 30px rgba(0,200,240,.4);padding-left:16px;line-height:1;margin-bottom:18px}
.copy-row{display:flex;gap:9px;justify-content:center}
.copy-btn{display:flex;align-items:center;gap:5px;background:var(--surface);border:1px solid var(--border);
  border-radius:9px;padding:8px 16px;font-size:.8rem;font-weight:700;color:var(--sub);transition:all .2s}
.copy-btn:hover{color:var(--text);border-color:var(--sub)}

/* QR row */
.qr-row{display:flex;gap:12px;margin-bottom:12px}
.qr-card{flex:1;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px 12px;
  text-align:center;display:flex;flex-direction:column;align-items:center;gap:9px;min-width:0}
.qr-card img{width:100%;max-width:190px;height:auto;border-radius:8px;aspect-ratio:1;border:2px solid var(--border)}
.qr-lbl{font-size:.73rem;font-weight:700;color:var(--sub)}
.qr-hint{font-size:.66rem;color:var(--sub);line-height:1.4}

/* Receivers */
.recv-section-title{font-size:.82rem;font-weight:700;color:var(--sub);margin-bottom:8px}
.recv-row{display:flex;flex-direction:column;gap:7px}
.recv-item{display:flex;align-items:center;gap:11px;background:var(--card);border:1px solid var(--border);border-radius:11px;padding:11px 13px}
.recv-avatar{width:32px;height:32px;border-radius:9px;flex-shrink:0;
  background:linear-gradient(135deg,var(--cyan),var(--purple));display:flex;align-items:center;justify-content:center;font-size:.82rem;font-weight:800;color:#fff}
.recv-info{flex:1;min-width:0}
.recv-name{font-size:.83rem;font-weight:700}
.recv-st{font-size:.7rem;color:var(--sub);margin-top:2px}
.recv-pct{font-size:.76rem;font-weight:700;color:var(--cyan);flex-shrink:0}
.recv-bar-wrap{width:100%;background:rgba(255,255,255,.06);border-radius:100px;height:4px;margin-top:6px;overflow:hidden}
.recv-bar{height:100%;border-radius:100px;background:linear-gradient(90deg,var(--cyan),var(--purple));width:0;transition:width .3s}

/* Wait spinner */
.wait-msg{display:flex;flex-direction:column;align-items:center;gap:9px;padding:22px;color:var(--sub);font-size:.83rem;text-align:center}
.spinner{width:30px;height:30px;border:3px solid var(--border);border-top-color:var(--cyan);border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* OTP input */
.otp-wrap{display:flex;gap:10px;justify-content:center;margin-bottom:22px}
.otp-box{width:62px;height:70px;background:var(--card);border:2px solid var(--border);border-radius:13px;
  font-size:1.9rem;font-weight:900;text-align:center;color:var(--text);font-family:'Courier New',monospace;
  transition:border-color .2s;line-height:70px}
.otp-box.filled{border-color:var(--cyan)}
.otp-hidden{position:absolute;opacity:0;pointer-events:none;width:1px;height:1px}

/* Recv progress */
.recv-prog-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:20px;margin-bottom:12px}
.recv-file-rows{display:flex;flex-direction:column;gap:11px;margin-bottom:4px}
.recv-file-row{display:flex;flex-direction:column;gap:5px}
.recv-file-name{font-size:.83rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.recv-file-meta{display:flex;align-items:center;justify-content:space-between;font-size:.7rem;color:var(--sub)}
.recv-file-bar-wrap{background:rgba(255,255,255,.06);border-radius:100px;height:5px;overflow:hidden}
.recv-file-bar{height:100%;border-radius:100px;background:linear-gradient(90deg,var(--purple),var(--cyan));width:0;transition:width .25s}

/* Overall */
.ov-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:18px 20px}
.ov-top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:9px}
.ov-title{font-size:.78rem;font-weight:700;color:var(--sub)}
.ov-pct{font-size:1.4rem;font-weight:900;color:var(--cyan)}
.ov-bar-wrap{background:rgba(255,255,255,.06);border-radius:100px;height:8px;overflow:hidden;margin-bottom:13px}
.ov-bar{height:100%;border-radius:100px;background:linear-gradient(90deg,var(--cyan),var(--purple),var(--green));width:0;transition:width .3s}
.metrics{display:flex;gap:9px;flex-wrap:wrap}
.metric{display:flex;align-items:center;gap:4px;background:rgba(255,255,255,.04);border:1px solid var(--border);
  border-radius:20px;padding:5px 11px;font-size:.73rem;font-weight:700;color:var(--sub)}
.metric span{color:var(--text)}

/* Notice */
.notice{display:flex;align-items:flex-start;gap:9px;border-radius:11px;padding:11px 15px;font-size:.8rem;line-height:1.5;margin:10px 0}
.n-info{background:rgba(68,136,255,.1);border:1px solid rgba(68,136,255,.2);color:#aabbff}
.n-ok  {background:rgba(0,217,138,.1);border:1px solid rgba(0,217,138,.2);color:var(--green)}
.n-err {background:rgba(255,77,109,.1);border:1px solid rgba(255,77,109,.2);color:#ff8899}
.n-warn{background:rgba(255,187,51,.1);border:1px solid rgba(255,187,51,.2);color:var(--amber)}

/* Privacy note */
.priv-note{display:flex;align-items:center;gap:7px;justify-content:center;font-size:.71rem;color:var(--sub);padding:14px 0 4px}

/* Gap util */
.gap{height:10px}
.gap2{height:6px}

/* Toast */
#toasts{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
  display:flex;flex-direction:column;gap:7px;align-items:center;z-index:1000;
  width:calc(100% - 32px);max-width:380px;pointer-events:none}
.toast{background:var(--card);border:1px solid var(--border);border-left:3px solid var(--cyan);
  padding:10px 16px;border-radius:11px;font-size:.81rem;font-weight:600;
  animation:tIn .22s ease;pointer-events:auto;width:100%;text-align:center}
@keyframes tIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}

/* Responsive */
@media(max-width:400px){
  .code-digits{font-size:2.8rem;letter-spacing:11px;padding-left:11px}
  .otp-box{width:52px;height:60px;font-size:1.6rem;line-height:60px}
  .qr-row{flex-direction:column}
  .qr-card img{max-width:220px;margin:0 auto}
}
@media(min-width:580px){
  .home-title{font-size:2.5rem}
  .mode-btns{flex-direction:row}
  .mode-btn{flex-direction:column;text-align:center;padding:26px 18px}
  .mba{display:none}
}
</style>
</head>
<body>

<!-- ═══ HOME PAGE ════════════════════════════════════════ -->
<div id="pgHome" class="page">
<div class="wrap">
  <div class="hdr">
    <div class="hdr-logo">ShareAnywhere</div>
    <div class="hdr-spacer"></div>
    <div class="dot" id="dot0"></div>
  </div>
  <div id="home">
    <div class="home-hero">
      <div class="home-icon">⚡</div>
      <h1 class="home-title">Send Files<br><span class="hl">Instantly</span></h1>
      <p class="home-sub">Share files directly between devices — no account, no storage, no hassle.</p>
      <div class="privacy-badge">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        Your files are never stored on any server
      </div>
      <div class="mode-btns">
        <button class="mode-btn wifi" onclick="pickMode('wifi')">
          <div class="mbi">📡</div>
          <div class="mbt">
            <div class="mbt-title">Same WiFi</div>
            <div class="mbt-desc">Ultra fast · Up to 5 GB<br>Both devices on same network</div>
          </div>
          <div class="mba">›</div>
        </button>
        <button class="mode-btn inet" onclick="pickMode('inet')">
          <div class="mbi">🌐</div>
          <div class="mbt">
            <div class="mbt-title">Over Internet</div>
            <div class="mbt-desc">Any network · Up to 500 MB<br>Works worldwide</div>
          </div>
          <div class="mba">›</div>
        </button>
      </div>
      <div class="spec-row">
        <div class="spec">🔒 Zero server upload</div>
        <div class="spec">📦 Multiple files</div>
        <div class="spec">👥 Multiple receivers</div>
        <div class="spec">📱 QR sharing</div>
        <div class="spec">🗑️ Auto-deleted after sharing</div>
      </div>
    </div>
  </div>
  <div class="footer">
    <div class="footer-text">
      <strong>ShareAnywhere</strong> — P2P File Share<br>
      Built with ❤️ by <strong>Mr. D.K. Chaudhary</strong>
      <span class="footer-divider">·</span>
      <a href="https://thedarkgeekdc2.github.io" target="_blank">@THEDARKGEEKDC</a><br>
      <span style="opacity:.6">No storage · No tracking · Files auto-deleted after transfer</span>
    </div>
  </div>
</div>
</div>

<!-- ═══ SEND PAGE ════════════════════════════════════════ -->
<div id="pgSend" class="page hidden">
<div class="wrap">
  <div class="hdr">
    <button class="hdr-back" onclick="goHome()">‹ Back</button>
    <div class="hdr-spacer"></div>
    <div class="dot on" id="dot1"></div>
  </div>

  <!-- Step 1: Pick files -->
  <div class="screen" id="stepPick">
    <div class="steps"><div class="step done"></div><div class="step half"></div><div class="step"></div></div>
    <div class="sec-title">Select Files</div>
    <div class="sec-sub">Add the files you want to share. Drag &amp; drop or tap to browse.</div>
    <div class="dz" id="dz">
      <input type="file" id="fi" multiple onchange="onFiles(this.files)">
      <div class="dz-icon">🗂️</div>
      <div class="dz-msg">Drop files here</div>
      <div class="dz-hint">or tap to browse</div>
    </div>
    <div class="file-list" id="fileList"></div>
    <div class="file-summary hidden" id="fileSummary">
      <span><strong id="fileCount">0</strong> files</span>
      <span>Total: <strong id="fileTotal">0 B</strong></span>
    </div>
    <div class="gap"></div>
    <button class="big-btn cyan" id="btnNext" onclick="goToShare()" disabled>Share these files →</button>
    <div class="gap2"></div>
    <button class="big-btn ghost" onclick="clearFiles()">Clear all</button>
    <div class="priv-note">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
      Files go directly to receiver — nothing stored on server
    </div>
  </div>

  <!-- Step 2: Code + QR -->
  <div class="screen hidden" id="stepShare">
    <div class="steps"><div class="step done"></div><div class="step done"></div><div class="step half" id="sp3s"></div></div>
    <div class="sec-title">Share this code</div>
    <div class="sec-sub">Receiver scans the QR or types the 4-digit code below.</div>
    <div class="code-card">
      <div class="code-label">YOUR CODE</div>
      <div class="code-digits" id="codeDigits">----</div>
      <div class="copy-row">
        <button class="copy-btn" onclick="copyCode()">📋 Copy Code</button>
        <button class="copy-btn" onclick="copyLink()">🔗 Copy Link</button>
      </div>
    </div>
    <div class="qr-row" id="qrRow"></div>
    <div class="recv-section-title" id="recvTitle">Waiting for receiver...</div>
    <div class="recv-row" id="recvList"></div>
    <div class="wait-msg" id="waitMsg"><div class="spinner"></div>Waiting for someone to connect...</div>
    <div class="priv-note">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
      Files transfer directly — auto-deleted after sharing
    </div>
  </div>
</div>
</div>

<!-- ═══ RECEIVE PAGE ════════════════════════════════════ -->
<div id="pgRecv" class="page hidden">
<div class="wrap">
  <div class="hdr">
    <button class="hdr-back" onclick="goHome()">‹ Back</button>
    <div class="hdr-spacer"></div>
    <div class="dot on"></div>
  </div>

  <!-- Code entry -->
  <div class="screen" id="stepCode">
    <div class="steps"><div class="step done"></div><div class="step half"></div><div class="step"></div></div>
    <div class="sec-title">Enter the code</div>
    <div class="sec-sub">Type the 4-digit code shown on the sender's screen, or scan their QR.</div>
    <input class="otp-hidden" type="tel" id="otpReal" maxlength="4"
      oninput="onOtpInput(this.value)" onkeydown="if(event.key==='Enter')joinRoom()"
      autocomplete="one-time-code" inputmode="numeric">
    <div class="otp-wrap" id="otpWrap" onclick="focusOtp()">
      <div class="otp-box" id="ob0">&nbsp;</div>
      <div class="otp-box" id="ob1">&nbsp;</div>
      <div class="otp-box" id="ob2">&nbsp;</div>
      <div class="otp-box" id="ob3">&nbsp;</div>
    </div>
    <button class="big-btn purple" id="btnJoin" onclick="joinRoom()" disabled>Connect to Sender →</button>
    <div class="priv-note" style="margin-top:18px">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
      Files save directly to your device — auto-deleted after transfer
    </div>
  </div>

  <!-- Progress -->
  <div class="screen hidden" id="stepRecvProg">
    <div class="steps"><div class="step done"></div><div class="step done"></div><div class="step half" id="sp3r"></div></div>
    <div class="sec-title">Receiving files</div>
    <div class="notice n-info" id="recvNotice"><span>⏳</span><span> Connecting to sender...</span></div>
    <div class="recv-prog-card hidden" id="recvProgCard">
      <div class="recv-file-rows" id="recvFileRows"></div>
    </div>
    <div class="ov-card hidden" id="ovCard">
      <div class="ov-top"><span class="ov-title">Overall Progress</span><span class="ov-pct" id="ovPct">0%</span></div>
      <div class="ov-bar-wrap"><div class="ov-bar" id="ovBar"></div></div>
      <div class="metrics">
        <div class="metric">⚡ Speed <span id="speedVal">—</span></div>
        <div class="metric">⏱ ETA <span id="etaVal">—</span></div>
        <div class="metric">📦 <span id="recvdVal">0 B</span></div>
      </div>
    </div>
    <div class="priv-note" style="margin-top:14px">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
      Nothing stored on server · Auto-deleted after transfer
    </div>
  </div>
</div>
</div>

<div id="toasts"></div>

<script>
const BASE=window.location.origin;
const CHUNK=256*1024,BLOCK=4*1024*1024;
const MAX_WIFI=5*1024*1024*1024,MAX_INET=500*1024*1024;
const ICE={iceServers:[{urls:'stun:stun.l.google.com:19302'},{urls:'stun:stun1.l.google.com:19302'},{urls:'stun:stun.cloudflare.com:3478'}]};

let socket,mode='wifi',code=null;
let selFiles=[],peers={},peerNum=0;
let rPC=null,senderSid=null,rBufs={},rPendHdr=null;
let rTotal=0,rTotalSz=0,speedTimer=null,lastB=0,lastT=0;
let localIP=null,localPort=null;

window.addEventListener('load',()=>{
  const p=new URLSearchParams(location.search);
  const c=p.get('code'),m=p.get('mode');
  fetch('/api/localip').then(r=>r.json()).then(d=>{localIP=d.ip;localPort=d.port;}).catch(()=>{});
  connectSocket();
  setupDZ();
  if(c){setTimeout(()=>{pickMode(m||'inet',false);showPage('pgRecv');show('stepCode');hide('stepRecvProg');document.getElementById('otpReal').value=c;onOtpInput(c);setTimeout(joinRoom,500);},400);}
});

function connectSocket(){
  socket=io(BASE,{transports:['websocket','polling']});
  socket.on('connect',()=>setOnline(true));
  socket.on('disconnect',()=>setOnline(false));
  socket.on('room_created',d=>onRoomCreated(d.code));
  socket.on('receiver_joined',d=>onReceiverJoined(d.receiver_sid));
  socket.on('webrtc_answer',d=>onSenderAnswer(d));
  socket.on('webrtc_ice',d=>onIce(d));
  socket.on('receiver_left',d=>onReceiverLeft(d.sid));
  socket.on('joined',d=>onJoined(d));
  socket.on('webrtc_offer',d=>onRecvOffer(d));
  socket.on('sender_left',()=>setNotice('err','❌ Sender disconnected.'));
  socket.on('error',d=>toast(d.msg,'err'));
}
function setOnline(v){document.querySelectorAll('.dot').forEach(el=>el.classList.toggle('on',v));}

function showPage(id){['pgHome','pgSend','pgRecv'].forEach(p=>document.getElementById(p).classList.toggle('hidden',p!==id));}
function goHome(){showPage('pgHome');resetAll();history.replaceState({},'','/');}
function pickMode(m,push=true){
  mode=m;showPage('pgSend');
  show('stepPick');hide('stepShare');
  if(push)history.replaceState({},`/?mode=${m}`);
}
function goToShare(){
  if(!selFiles.length)return;
  const btn=document.getElementById('btnNext');
  btn.disabled=true;btn.textContent='Creating room...';
  socket.emit('create_room');
}

function setupDZ(){
  const dz=document.getElementById('dz');
  dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('over');});
  dz.addEventListener('dragleave',()=>dz.classList.remove('over'));
  dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('over');onFiles(e.dataTransfer.files);});
}
function onFiles(fl){
  const max=mode==='wifi'?MAX_WIFI:MAX_INET;
  for(const f of fl){if(f.size>max){toast(`"${f.name}" too large for ${mode==='wifi'?'WiFi (5 GB)':'Internet (500 MB)'}`, 'warn');continue;}selFiles.push(f);}
  renderFiles();
}
function clearFiles(){selFiles=[];document.getElementById('fi').value='';renderFiles();}
function removeFile(i){selFiles.splice(i,1);renderFiles();}
function renderFiles(){
  const tot=selFiles.reduce((a,f)=>a+f.size,0);
  document.getElementById('fileList').innerHTML=selFiles.map((f,i)=>`
    <div class="file-item">
      <span class="file-ico">${icon(f.name)}</span>
      <div class="file-info"><div class="file-name">${esc(f.name)}</div><div class="file-size">${sz(f.size)}</div></div>
      <button class="file-del" onclick="removeFile(${i})">✕</button>
    </div>`).join('');
  const has=selFiles.length>0;
  document.getElementById('fileSummary').classList.toggle('hidden',!has);
  if(has){document.getElementById('fileCount').textContent=selFiles.length;document.getElementById('fileTotal').textContent=sz(tot);}
  const btn=document.getElementById('btnNext');
  btn.disabled=!has;
  if(has)btn.textContent='Share these files →';
}

function onRoomCreated(c){
  code=c;
  document.getElementById('codeDigits').textContent=c;
  buildQR(c);
  hide('stepPick');show('stepShare');
}
function buildQR(c){
  const row=document.getElementById('qrRow');row.innerHTML='';
  const inetURL=`${BASE}/?code=${c}&mode=inet`;
  if(mode==='wifi'&&localIP){
    const wifiURL=`http://${localIP}:${localPort}/?code=${c}&mode=wifi`;
    row.innerHTML+=qrCard(wifiURL,'📡 WiFi QR','Scan on same WiFi');
    row.innerHTML+=qrCard(inetURL,'🌐 Internet QR','Any network');
  } else {
    row.style.justifyContent='center';
    row.innerHTML+=qrCard(inetURL,'🌐 Share QR','Scan or enter code');
  }
}
function qrCard(url,label,hint){
  return `<div class="qr-card"><img src="/qr?d=${encodeURIComponent(url)}&s=10" alt="QR" loading="lazy"><div class="qr-lbl">${label}</div><div class="qr-hint">${hint}</div></div>`;
}

async function onReceiverJoined(sid){
  peerNum++;const num=peerNum;
  addRecvRow(sid,num);hide('waitMsg');
  document.getElementById('recvTitle').textContent=`Connected (${Object.keys(peers).length+1})`;
  await initSenderPeer(sid,num);
}
async function initSenderPeer(sid,num){
  const pc=new RTCPeerConnection(ICE);
  const dc=pc.createDataChannel('share',{ordered:true});
  peers[sid]={pc,dc,sent:0,total:0,num};
  dc.bufferedAmountLowThreshold=CHUNK*4;
  dc.onopen=()=>{setRecvSt(sid,'Transferring...','active');sendAll(sid);};
  dc.onerror=()=>setRecvSt(sid,'Error','lost');
  pc.onicecandidate=e=>{if(e.candidate)socket.emit('webrtc_ice',{to:sid,candidate:e.candidate});};
  pc.onconnectionstatechange=()=>{if(['failed','disconnected'].includes(pc.connectionState))setRecvSt(sid,'Lost','lost');};
  const offer=await pc.createOffer();
  await pc.setLocalDescription(offer);
  socket.emit('webrtc_offer',{to:sid,offer:pc.localDescription});
}
async function onSenderAnswer(d){const p=peers[d.from];if(p)await p.pc.setRemoteDescription(new RTCSessionDescription(d.answer));}
function onReceiverLeft(sid){if(peers[sid]){setRecvSt(sid,'Disconnected','lost');try{peers[sid].pc.close();}catch(e){}delete peers[sid];}}

async function sendAll(sid){
  const p=peers[sid];if(!p)return;
  const total=selFiles.reduce((a,f)=>a+f.size,0);p.total=total;
  p.dc.send(JSON.stringify({type:'meta',totalSize:total,files:selFiles.map((f,i)=>({index:i,name:f.name,size:f.size,mime:f.type||'application/octet-stream'}))}));
  for(let i=0;i<selFiles.length;i++){if(!peers[sid])return;await sendFile(p.dc,selFiles[i],i,sid);}
  p.dc.send(JSON.stringify({type:'all_done'}));
  setRecvSt(sid,'Done ✅','done');setRecvProg(sid,100);
}
async function sendFile(dc,file,idx,sid){
  dc.send(JSON.stringify({type:'file_start',idx,name:file.name,size:file.size}));
  let off=0;
  while(off<file.size){
    const buf=await file.slice(off,Math.min(off+BLOCK,file.size)).arrayBuffer();
    let bo=0;
    while(bo<buf.byteLength){
      if(dc.bufferedAmount>CHUNK*8)await new Promise(res=>{dc.bufferedAmountLowThreshold=CHUNK*2;dc.onbufferedamountlow=()=>{dc.onbufferedamountlow=null;res();};});
      const chunk=buf.slice(bo,Math.min(bo+CHUNK,buf.byteLength));
      dc.send(JSON.stringify({type:'chunk',idx,offset:off+bo,sz:chunk.byteLength}));
      dc.send(chunk);
      bo+=chunk.byteLength;
      if(peers[sid]){peers[sid].sent+=chunk.byteLength;setRecvProg(sid,Math.round(peers[sid].sent/peers[sid].total*100));}
    }
    off+=buf.byteLength;
  }
  dc.send(JSON.stringify({type:'file_done',idx}));
}

function addRecvRow(sid,num){
  const el=document.createElement('div');el.className='recv-item';el.id=`ri-${sid}`;
  el.innerHTML=`<div class="recv-avatar">${num}</div>
    <div class="recv-info" style="flex:1;min-width:0">
      <div class="recv-name">Receiver ${num}</div>
      <div class="recv-st" id="rst-${sid}">Connecting...</div>
      <div class="recv-bar-wrap"><div class="recv-bar" id="rb-${sid}"></div></div>
    </div>
    <div class="recv-pct" id="rp-${sid}">0%</div>`;
  document.getElementById('recvList').appendChild(el);
}
function setRecvSt(sid,txt,state){
  const s=document.getElementById(`rst-${sid}`);if(s)s.textContent=txt;
  const b=document.getElementById(`rb-${sid}`);
  if(b)b.style.background=state==='done'?'var(--green)':state==='lost'?'var(--red)':'linear-gradient(90deg,var(--cyan),var(--purple))';
}
function setRecvProg(sid,pct){
  const b=document.getElementById(`rb-${sid}`);if(b)b.style.width=pct+'%';
  const p=document.getElementById(`rp-${sid}`);if(p)p.textContent=pct+'%';
}

function focusOtp(){document.getElementById('otpReal').focus();}
function onOtpInput(v){
  const d=v.replace(/\D/g,'').slice(0,4);
  for(let i=0;i<4;i++){
    const box=document.getElementById(`ob${i}`);
    box.textContent=d[i]||'\u00a0';
    box.classList.toggle('filled',!!d[i]);
  }
  document.getElementById('btnJoin').disabled=d.length<4;
}
function joinRoom(){
  const c=document.getElementById('otpReal').value.trim();
  if(!/^\d{4}$/.test(c)){toast('Please enter all 4 digits.','warn');return;}
  socket.emit('join_room_request',{code:c});
}
function onJoined(d){senderSid=d.sender_sid;hide('stepCode');show('stepRecvProg');}

async function onRecvOffer(d){
  senderSid=d.from;
  rPC=new RTCPeerConnection(ICE);
  rPC.onicecandidate=e=>{if(e.candidate)socket.emit('webrtc_ice',{to:senderSid,candidate:e.candidate});};
  rPC.ondatachannel=e=>setupRecvDC(e.channel);
  rPC.onconnectionstatechange=()=>{
    if(rPC.connectionState==='connected')setNotice('ok','✅ Connected! Receiving files...');
    if(rPC.connectionState==='failed')setNotice('err','❌ Connection failed. Try again.');
  };
  await rPC.setRemoteDescription(new RTCSessionDescription(d.offer));
  const ans=await rPC.createAnswer();
  await rPC.setLocalDescription(ans);
  socket.emit('webrtc_answer',{to:senderSid,answer:rPC.localDescription});
}
function setupRecvDC(dc){
  dc.binaryType='arraybuffer';
  dc.onmessage=e=>{typeof e.data==='string'?handleCtrl(JSON.parse(e.data)):handleBin(e.data);};
}
function handleCtrl(m){
  if(m.type==='meta'){
    rBufs={};rTotalSz=m.totalSize;rTotal=0;lastB=0;lastT=Date.now();
    m.files.forEach(f=>{rBufs[f.index]={meta:f,chunks:[],got:0};});
    renderRecvFiles(m.files);
    show('recvProgCard');show('ovCard');startSpeed();
  }
  if(m.type==='chunk')rPendHdr=m;
  if(m.type==='file_done')fileDone(m.idx);
  if(m.type==='all_done')allDone();
}
function handleBin(data){
  if(!rPendHdr)return;
  const{idx}=rPendHdr;rPendHdr=null;
  const buf=rBufs[idx];if(!buf)return;
  buf.chunks.push(data);buf.got+=data.byteLength;rTotal+=data.byteLength;
  const pct=Math.round(rTotal/rTotalSz*100);
  document.getElementById('ovPct').textContent=pct+'%';
  document.getElementById('ovBar').style.width=pct+'%';
  document.getElementById('recvdVal').textContent=`${sz(rTotal)} / ${sz(rTotalSz)}`;
  const fp=Math.round(buf.got/buf.meta.size*100);
  const fb=document.getElementById(`rfb-${idx}`);if(fb)fb.style.width=fp+'%';
  const fm=document.getElementById(`rfm-${idx}`);if(fm)fm.textContent=`${sz(buf.got)} / ${sz(buf.meta.size)}`;
}
function fileDone(idx){
  const fb=document.getElementById(`rfb-${idx}`);if(fb)fb.style.width='100%';
  const fm=document.getElementById(`rfm-${idx}`);if(fm)fm.textContent='✅ Done';
}
async function allDone(){
  stopSpeed();setNotice('ok','✅ Transfer complete! Preparing download...');
  document.getElementById('sp3r').style.background='var(--cyan)';document.getElementById('sp3r').style.opacity='1';
  const all=Object.values(rBufs);
  if(all.length===1){dlBlob(new Blob(all[0].chunks,{type:all[0].meta.mime}),all[0].meta.name);}
  else{
    setNotice('info','📦 Creating ZIP archive...');
    const zip=new JSZip();
    all.forEach(f=>zip.file(f.meta.name,new Blob(f.chunks)));
    const blob=await zip.generateAsync({type:'blob',compression:'DEFLATE',compressionOptions:{level:1}},m=>setNotice('info',`📦 ZIP ${m.percent.toFixed(0)}%`));
    dlBlob(blob,'shared_files.zip');
  }
  setNotice('ok','🎉 Download started! Check your Downloads folder.');
}
function dlBlob(blob,name){
  const url=URL.createObjectURL(blob);
  const a=Object.assign(document.createElement('a'),{href:url,download:name});
  document.body.appendChild(a);a.click();document.body.removeChild(a);
  setTimeout(()=>URL.revokeObjectURL(url),15000);
}
function renderRecvFiles(files){
  document.getElementById('recvFileRows').innerHTML=files.map(f=>`
    <div class="recv-file-row">
      <div class="recv-file-name">${icon(f.name)} ${esc(f.name)}</div>
      <div class="recv-file-meta"><span id="rfm-${f.index}">0 B / ${sz(f.size)}</span></div>
      <div class="recv-file-bar-wrap"><div class="recv-file-bar" id="rfb-${f.index}"></div></div>
    </div>`).join('');
}

async function onIce(d){
  if(peers[d.from]){try{await peers[d.from].pc.addIceCandidate(new RTCIceCandidate(d.candidate));}catch(e){}return;}
  if(rPC&&d.from===senderSid){try{await rPC.addIceCandidate(new RTCIceCandidate(d.candidate));}catch(e){}}
}
function startSpeed(){lastB=0;lastT=Date.now();speedTimer=setInterval(()=>{const now=Date.now(),dt=(now-lastT)/1000,bps=(rTotal-lastB)/dt;lastB=rTotal;lastT=now;document.getElementById('speedVal').textContent=spd(bps);if(bps>0&&rTotalSz>rTotal){document.getElementById('etaVal').textContent=fmtT((rTotalSz-rTotal)/bps);}},1000);}
function stopSpeed(){if(speedTimer){clearInterval(speedTimer);speedTimer=null;}}
function setNotice(type,msg){
  const cls={ok:'n-ok',err:'n-err',warn:'n-warn',info:'n-info'}[type]||'n-info';
  const ic={ok:'✅',err:'❌',warn:'⚠️',info:'ℹ️'}[type]||'ℹ️';
  const el=document.getElementById('recvNotice');
  el.className=`notice ${cls}`;el.innerHTML=`<span>${ic}</span><span> ${msg}</span>`;
}
function copyCode(){navigator.clipboard?.writeText(code);toast('Code copied: '+code);}
function copyLink(){navigator.clipboard?.writeText(`${BASE}/?code=${code}&mode=${mode}`);toast('Link copied!');}
function toast(msg,type='ok'){
  const area=document.getElementById('toasts');
  const el=document.createElement('div');el.className='toast';
  el.style.borderLeftColor=type==='err'?'var(--red)':type==='warn'?'var(--amber)':'var(--cyan)';
  el.textContent=msg;area.appendChild(el);
  setTimeout(()=>{el.style.opacity='0';el.style.transition='opacity .3s';setTimeout(()=>el.remove(),300);},3000);
}
function show(id){document.getElementById(id)?.classList.remove('hidden');}
function hide(id){document.getElementById(id)?.classList.add('hidden');}
function resetAll(){selFiles=[];peers={};peerNum=0;rPC=null;rBufs={};rTotal=0;code=null;stopSpeed();}
function sz(b){if(b<1024)return b+' B';if(b<1048576)return(b/1024).toFixed(1)+' KB';if(b<1073741824)return(b/1048576).toFixed(1)+' MB';return(b/1073741824).toFixed(2)+' GB';}
function spd(b){if(b<1024)return b.toFixed(0)+' B/s';if(b<1048576)return(b/1024).toFixed(1)+' KB/s';return(b/1048576).toFixed(1)+' MB/s';}
function fmtT(s){if(s<60)return s.toFixed(0)+'s';if(s<3600)return Math.floor(s/60)+'m '+Math.floor(s%60)+'s';return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function icon(n){const e=n.split('.').pop().toLowerCase();return({pdf:'📄',doc:'📝',docx:'📝',txt:'📝',jpg:'🖼',jpeg:'🖼',png:'🖼',gif:'🖼',webp:'🖼',mp4:'🎬',mkv:'🎬',avi:'🎬',mov:'🎬',mp3:'🎵',wav:'🎵',flac:'🎵',m4a:'🎵',zip:'📦',rar:'📦','7z':'📦',apk:'📱',exe:'⚙',py:'🐍',js:'📜',html:'🌐',xlsx:'📊',csv:'📊',pptx:'📊',iso:'💿'}[e]||'📁');}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────
#  STARTUP
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    local_ip = get_local_ip()
    print(f"""
╔══════════════════════════════════════════════════════╗
║   ⚡  ShareAnywhere — P2P File Share  v3.0          ║
╠══════════════════════════════════════════════════════╣
║  📶  Local :  http://{local_ip}:{PORT:<5}               ║
║  🌐  Public:  Deploy on Railway for internet use    ║
║  🚀  Mode  :  WebRTC Direct · Zero Upload           ║
║  👤  Dev   :  @thedarkgeekdc (Mr D.K. CHAUDHARY)    ║
╚══════════════════════════════════════════════════════╝
    """)
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
