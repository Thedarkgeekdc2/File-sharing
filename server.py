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
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>P2P Share — THEDARKGEEKDC</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>
<style>
/* ── Reset & Variables ─────────────────────────────────── */
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:       #050510;
  --surface:  #0a0a1e;
  --card:     #0d0d28;
  --card2:    #111132;
  --border:   #1e1e4a;
  --border2:  #2a2a5a;
  --cyan:     #00d4ff;
  --cyan2:    #00a8cc;
  --blue:     #4466ff;
  --purple:   #8844ff;
  --green:    #00e5a0;
  --red:      #ff4466;
  --amber:    #ffaa00;
  --text:     #e8e8ff;
  --muted:    #6a6a9a;
  --muted2:   #4a4a7a;
  --glow:     0 0 24px rgba(0,212,255,.18);
}
html{scroll-behavior:smooth}
body{
  font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
  background:var(--bg);color:var(--text);min-height:100vh;
  background-image:
    radial-gradient(ellipse 80% 60% at 50% -10%,rgba(0,212,255,.07),transparent),
    radial-gradient(ellipse 60% 40% at 90% 110%,rgba(136,68,255,.05),transparent);
}

/* ── Scrollbar ─────────────────────────────────────────── */
::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:8px}

/* ── Header ─────────────────────────────────────────────── */
.hdr{
  position:sticky;top:0;z-index:100;
  background:rgba(5,5,16,.88);backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  padding:14px 20px;display:flex;align-items:center;gap:14px;
}
.hdr-logo{
  font-size:1.1rem;font-weight:900;letter-spacing:3px;
  background:linear-gradient(135deg,var(--cyan),var(--blue),var(--purple));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.hdr-badge{
  font-size:.62rem;font-weight:800;letter-spacing:1px;
  border:1px solid var(--cyan);color:var(--cyan);
  padding:3px 9px;border-radius:20px;
}
.hdr-spacer{flex:1}
.conn-dot{
  width:8px;height:8px;border-radius:50%;background:var(--muted2);
  transition:background .4s;flex-shrink:0;
}
.conn-dot.online{background:var(--green);box-shadow:0 0 8px var(--green)}
.conn-lbl{font-size:.72rem;color:var(--muted)}

/* ── Hero / Landing ──────────────────────────────────────── */
.hero{
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  min-height:calc(100vh - 57px);padding:40px 20px;text-align:center;
}
.hero-badge{
  display:inline-flex;align-items:center;gap:7px;
  background:rgba(0,212,255,.08);border:1px solid rgba(0,212,255,.25);
  border-radius:40px;padding:6px 16px;font-size:.75rem;font-weight:700;
  color:var(--cyan);letter-spacing:1px;margin-bottom:28px;
}
.hero-badge::before{
  content:'';width:6px;height:6px;border-radius:50%;
  background:var(--cyan);animation:blink 1.5s infinite;flex-shrink:0;
}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}

.hero-title{
  font-size:clamp(2rem,6vw,3.8rem);font-weight:900;line-height:1.1;
  margin-bottom:16px;letter-spacing:-1px;
}
.hero-title .g1{
  background:linear-gradient(135deg,var(--cyan),var(--blue));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.hero-title .g2{
  background:linear-gradient(135deg,var(--blue),var(--purple));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.hero-sub{
  font-size:1rem;color:var(--muted);max-width:480px;line-height:1.65;margin-bottom:48px;
}
.hero-sub strong{color:var(--cyan)}

/* ── Mode Cards ──────────────────────────────────────────── */
.mode-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;width:100%;max-width:680px;margin-bottom:48px}

.mode-card{
  position:relative;overflow:hidden;
  background:var(--card);border:1.5px solid var(--border);
  border-radius:20px;padding:28px 22px;text-align:left;
  cursor:pointer;transition:all .25s;
}
.mode-card::before{
  content:'';position:absolute;inset:0;
  background:linear-gradient(135deg,transparent,rgba(255,255,255,.02));
  pointer-events:none;
}
.mode-card:hover{transform:translateY(-4px);border-color:var(--border2);box-shadow:var(--glow)}
.mode-card.wifi:hover{border-color:var(--cyan);box-shadow:0 0 32px rgba(0,212,255,.2)}
.mode-card.inet:hover{border-color:var(--purple);box-shadow:0 0 32px rgba(136,68,255,.2)}

.mc-icon{
  width:54px;height:54px;border-radius:14px;
  display:flex;align-items:center;justify-content:center;
  font-size:1.6rem;margin-bottom:16px;
}
.wifi .mc-icon{background:rgba(0,212,255,.1);border:1px solid rgba(0,212,255,.2)}
.inet .mc-icon{background:rgba(136,68,255,.1);border:1px solid rgba(136,68,255,.2)}

.mc-title{font-size:1.05rem;font-weight:800;margin-bottom:6px}
.wifi .mc-title{color:var(--cyan)}
.inet .mc-title{color:#aa88ff}

.mc-desc{font-size:.8rem;color:var(--muted);line-height:1.55;margin-bottom:16px}

.mc-stats{display:flex;gap:8px;flex-wrap:wrap}
.mc-stat{font-size:.68rem;font-weight:700;padding:3px 10px;border-radius:20px}
.wifi .mc-stat{background:rgba(0,212,255,.1);color:var(--cyan);border:1px solid rgba(0,212,255,.2)}
.inet .mc-stat{background:rgba(136,68,255,.1);color:#aa88ff;border:1px solid rgba(136,68,255,.2)}

.mc-arrow{
  position:absolute;right:18px;top:50%;transform:translateY(-50%);
  font-size:1.3rem;opacity:.25;transition:all .25s;
}
.mode-card:hover .mc-arrow{opacity:1;right:14px}

/* ── Hero Features ─────────────────────────────────────── */
.feat-row{display:flex;gap:24px;flex-wrap:wrap;justify-content:center}
.feat{display:flex;align-items:center;gap:8px;font-size:.78rem;color:var(--muted)}

/* ── App Shell ─────────────────────────────────────────── */
.app{max-width:900px;margin:0 auto;padding:24px 16px;display:none}
.app.active{display:block}

/* ── Breadcrumb ─────────────────────────────────────────── */
.breadcrumb{
  display:flex;align-items:center;gap:8px;
  font-size:.82rem;color:var(--muted);margin-bottom:22px;flex-wrap:wrap;
}
.bc-link{cursor:pointer;color:var(--muted);transition:color .2s}
.bc-link:hover{color:var(--cyan)}
.bc-sep{color:var(--muted2)}
.bc-cur{color:var(--text);font-weight:700}
.mode-indicator{
  margin-left:auto;display:flex;align-items:center;gap:6px;
  font-size:.72rem;font-weight:700;padding:4px 12px;border-radius:20px;
}
.mode-indicator.wifi{background:rgba(0,212,255,.1);color:var(--cyan);border:1px solid rgba(0,212,255,.2)}
.mode-indicator.inet{background:rgba(136,68,255,.1);color:#aa88ff;border:1px solid rgba(136,68,255,.2)}

/* ── Tabs ───────────────────────────────────────────────── */
.tabs{display:flex;gap:8px;margin-bottom:22px;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:6px}
.tab{
  flex:1;padding:12px;border:none;border-radius:10px;
  background:transparent;color:var(--muted);cursor:pointer;
  font-weight:700;font-size:.9rem;transition:all .2s;
}
.tab.on{background:var(--card2);color:var(--text);box-shadow:0 2px 12px rgba(0,0,0,.4)}
.tab.on.send-tab{color:var(--cyan)}
.tab.on.recv-tab{color:#aa88ff}

/* ── Card ───────────────────────────────────────────────── */
.card{
  background:var(--card);border:1px solid var(--border);
  border-radius:18px;padding:24px;margin-bottom:16px;
}
.card-hdr{
  display:flex;align-items:center;gap:10px;
  font-size:.9rem;font-weight:700;color:var(--text);
  margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid var(--border);
}
.card-hdr-icon{
  width:34px;height:34px;border-radius:9px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;font-size:1rem;
  background:rgba(0,212,255,.1);
}

/* ── Drop Zone ──────────────────────────────────────────── */
.dz{
  border:2px dashed var(--border2);border-radius:14px;
  padding:44px 20px;text-align:center;cursor:pointer;
  transition:all .2s;position:relative;overflow:hidden;
}
.dz:hover,.dz.over{border-color:var(--cyan);background:rgba(0,212,255,.03)}
.dz input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.dz-emoji{font-size:3rem;margin-bottom:12px;display:block}
.dz-title{font-size:1rem;font-weight:700;margin-bottom:6px}
.dz-sub{font-size:.78rem;color:var(--muted)}
.dz-pill{
  display:inline-block;padding:2px 10px;border-radius:20px;font-weight:700;
  font-size:.72rem;background:rgba(0,212,255,.1);color:var(--cyan);
  border:1px solid rgba(0,212,255,.2);margin:0 3px;
}

/* ── File List ──────────────────────────────────────────── */
.fl-wrap{margin-top:14px;display:flex;flex-direction:column;gap:8px}
.fl-item{
  display:flex;align-items:center;gap:11px;
  background:rgba(255,255,255,.03);border:1px solid var(--border);
  border-radius:11px;padding:10px 12px;
}
.fl-ico{font-size:1.6rem;flex-shrink:0}
.fl-inf{flex:1;min-width:0}
.fl-nm{font-size:.85rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fl-meta{font-size:.73rem;color:var(--muted);margin-top:2px}
.fl-rm{background:none;border:none;color:var(--red);cursor:pointer;font-size:1rem;padding:5px;line-height:1;opacity:.5;transition:opacity .2s;flex-shrink:0}
.fl-rm:hover{opacity:1}

/* ── Actions Bar ─────────────────────────────────────────── */
.act-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
.total-lbl{font-size:.8rem;color:var(--muted)}

/* ── Buttons ─────────────────────────────────────────────── */
.btn{
  display:inline-flex;align-items:center;gap:7px;
  padding:11px 20px;border:none;border-radius:11px;
  font-size:.88rem;font-weight:700;cursor:pointer;transition:all .2s;font-family:inherit;
}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none!important}

.btn-primary{background:linear-gradient(135deg,var(--cyan),var(--blue));color:#000}
.btn-primary:not(:disabled):hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,212,255,.35)}

.btn-purple{background:linear-gradient(135deg,var(--blue),var(--purple));color:#fff}
.btn-purple:not(:disabled):hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(136,68,255,.35)}

.btn-ghost{background:rgba(255,255,255,.05);color:var(--muted);border:1px solid var(--border)}
.btn-ghost:hover{background:rgba(255,255,255,.08);color:var(--text)}

.btn-danger{background:rgba(255,68,102,.1);color:var(--red);border:1.5px solid rgba(255,68,102,.3)}
.btn-danger:hover{background:rgba(255,68,102,.2);border-color:var(--red)}

/* ── Stop Button (shown during active transfer) ─────────── */
#stopBtn{display:none}
#stopBtn.active{display:inline-flex}

/* ── Share Card (code + QR) ──────────────────────────────── */
.code-display{
  display:flex;align-items:center;gap:16px;flex-wrap:wrap;
  background:rgba(0,212,255,.05);border:1.5px solid rgba(0,212,255,.2);
  border-radius:14px;padding:18px 20px;margin:14px 0;
}
.code-num{
  font-family:'Courier New',monospace;font-size:3rem;font-weight:900;
  letter-spacing:14px;color:var(--cyan);text-shadow:0 0 20px rgba(0,212,255,.4);
}
.code-lbl{font-size:.68rem;color:var(--muted);margin-bottom:4px;font-weight:600;letter-spacing:1px}
.code-actions{display:flex;gap:8px;flex-wrap:wrap}

.qr-section{display:flex;gap:20px;flex-wrap:wrap;margin-top:16px;align-items:flex-start}
.qr-box{background:var(--card2);border:1px solid var(--border);border-radius:14px;padding:14px;text-align:center}
.qr-box img{width:150px;height:150px;border-radius:8px;display:block}
.qr-lbl{font-size:.72rem;color:var(--muted);margin-top:8px}
.qr-lbl strong{color:var(--text)}

/* ── Receiver List ───────────────────────────────────────── */
.rv-list{display:flex;flex-direction:column;gap:8px}
.rv-item{
  display:flex;align-items:center;gap:12px;
  background:rgba(255,255,255,.03);border:1px solid var(--border);
  border-radius:11px;padding:12px 14px;
}
.rv-avatar{
  width:36px;height:36px;border-radius:10px;flex-shrink:0;
  background:linear-gradient(135deg,var(--blue),var(--purple));
  display:flex;align-items:center;justify-content:center;
  font-size:.85rem;font-weight:800;color:#fff;
}
.rv-info{flex:1;min-width:0}
.rv-name{font-size:.85rem;font-weight:700}
.rv-status{font-size:.73rem;color:var(--muted);margin-top:2px}
.rv-prog-wrap{min-width:120px}
.prog-bar-outer{background:rgba(255,255,255,.06);border-radius:100px;height:6px;overflow:hidden}
.prog-bar-inner{
  height:100%;border-radius:100px;
  background:linear-gradient(90deg,var(--blue),var(--cyan));
  transition:width .3s ease;width:0;
}
.prog-pct{font-size:.72rem;color:var(--muted);margin-top:4px;text-align:right}

.rv-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;background:var(--muted2);transition:background .3s}
.rv-dot.connecting{background:var(--amber);animation:dotpulse .8s infinite}
.rv-dot.active{background:var(--green);box-shadow:0 0 8px var(--green)}
.rv-dot.done{background:var(--cyan)}
.rv-dot.lost{background:var(--red)}
@keyframes dotpulse{0%,100%{opacity:1}50%{opacity:.2}}

/* ── Wait State ──────────────────────────────────────────── */
.wait-box{
  display:flex;flex-direction:column;align-items:center;gap:8px;
  padding:28px;text-align:center;color:var(--muted);font-size:.85rem;
}
.wait-spinner{
  width:36px;height:36px;border:3px solid var(--border2);
  border-top-color:var(--cyan);border-radius:50%;
  animation:spin 1s linear infinite;margin-bottom:6px;
}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── Code Input ──────────────────────────────────────────── */
.code-input-wrap{display:flex;gap:10px;align-items:stretch}
.code-input{
  flex:1;padding:16px;background:var(--card2);
  border:1.5px solid var(--border2);border-radius:11px;
  color:white;font-size:2rem;font-weight:800;
  letter-spacing:12px;text-align:center;font-family:'Courier New',monospace;
  transition:border-color .2s;font-family:inherit;
}
.code-input:focus{outline:none;border-color:var(--cyan)}
.code-input::placeholder{color:var(--muted2);letter-spacing:4px;font-size:1.2rem}

/* ── Status Messages ─────────────────────────────────────── */
.status{
  display:flex;align-items:flex-start;gap:10px;
  padding:12px 16px;border-radius:11px;font-size:.84rem;
  line-height:1.55;margin:10px 0;
}
.status-info{background:rgba(68,136,255,.1);border:1px solid rgba(68,136,255,.25);color:#aabbff}
.status-ok{background:rgba(0,229,160,.1);border:1px solid rgba(0,229,160,.25);color:var(--green)}
.status-err{background:rgba(255,68,102,.1);border:1px solid rgba(255,68,102,.25);color:#ff8899}
.status-warn{background:rgba(255,170,0,.1);border:1px solid rgba(255,170,0,.25);color:var(--amber)}
.status-icon{flex-shrink:0;font-size:1rem;margin-top:1px}

/* ── Recv Progress ───────────────────────────────────────── */
.recv-files{display:flex;flex-direction:column;gap:8px;margin-bottom:14px}
.rf-item{
  background:rgba(255,255,255,.03);border:1px solid var(--border);
  border-radius:11px;padding:12px;
}
.rf-header{display:flex;align-items:center;gap:10px}
.rf-info{flex:1;min-width:0}
.rf-nm{font-size:.84rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rf-meta{font-size:.72rem;color:var(--muted);margin-top:2px}
.rf-prog-bar{background:rgba(255,255,255,.06);border-radius:100px;height:5px;overflow:hidden;margin-top:9px}
.rf-prog-fill{
  height:100%;border-radius:100px;width:0;
  background:linear-gradient(90deg,var(--purple),var(--cyan));
  transition:width .25s ease;
}

/* ── Speed / ETA ─────────────────────────────────────────── */
.metrics-row{display:flex;gap:12px;flex-wrap:wrap;margin-top:12px}
.metric-pill{
  display:inline-flex;align-items:center;gap:5px;
  padding:5px 14px;border-radius:20px;font-size:.78rem;font-weight:700;
  background:rgba(0,212,255,.08);color:var(--cyan);
  border:1px solid rgba(0,212,255,.2);
}

/* ── Overall Bar ─────────────────────────────────────────── */
.ov-section{margin-top:16px}
.ov-header{display:flex;justify-content:space-between;font-size:.78rem;color:var(--muted);margin-bottom:7px}
.ov-outer{background:rgba(255,255,255,.06);border-radius:100px;height:9px;overflow:hidden}
.ov-inner{
  height:100%;border-radius:100px;
  background:linear-gradient(90deg,var(--blue),var(--cyan),var(--green));
  transition:width .3s ease;width:0;
}

/* ── Toast ───────────────────────────────────────────────── */
#toastArea{position:fixed;bottom:24px;right:20px;display:flex;flex-direction:column;gap:8px;z-index:9999}
.toast-item{
  background:var(--card2);border:1px solid var(--border2);
  border-left:3px solid var(--cyan);
  padding:11px 16px;border-radius:10px;font-size:.83rem;font-weight:600;
  animation:toastIn .25s ease;box-shadow:0 8px 24px rgba(0,0,0,.5);
  max-width:300px;
}
@keyframes toastIn{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:translateX(0)}}

.hidden{display:none!important}

/* ── Footer ──────────────────────────────────────────────── */
footer{
  text-align:center;padding:28px 16px 24px;
  font-size:.73rem;color:var(--muted2);
  border-top:1px solid var(--border);margin-top:32px;
  background:rgba(255,255,255,.012);
}
footer a{color:var(--cyan);text-decoration:none;transition:opacity .2s}
footer a:hover{opacity:.75}
.footer-brand{
  font-size:.82rem;font-weight:800;letter-spacing:2px;
  background:linear-gradient(135deg,var(--cyan),var(--blue),var(--purple));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
  display:inline-block;margin-bottom:6px;
}
.footer-dots{margin:0 6px;opacity:.35}
.footer-dev{font-size:.72rem;color:var(--muted);margin-top:6px}
.footer-dev strong{color:var(--text)}

/* ── Responsive ──────────────────────────────────────────── */
@media(max-width:560px){
  .mode-grid{grid-template-columns:1fr}
  .mc-arrow{display:none}
  .qr-section{gap:12px}
  .qr-box img{width:130px;height:130px}
}
@media(max-width:480px){
  .hdr{padding:10px 14px}
  .hdr-logo{font-size:1rem;letter-spacing:2px}
  .hdr-badge{font-size:.58rem;padding:2px 7px}
  .hero{padding:28px 14px;min-height:calc(100vh - 50px)}
  .mode-card{padding:20px 16px;border-radius:16px}
  .mc-icon{width:44px;height:44px;font-size:1.3rem}
  .tabs{gap:5px;padding:5px}
  .tab{padding:10px 6px;font-size:.82rem}
  .card{padding:16px}
  .code-num{font-size:2.4rem;letter-spacing:10px}
  .btn{padding:9px 14px;font-size:.82rem}
  .code-display{padding:14px;gap:12px}
  .act-bar{gap:8px}
  .footer-brand{font-size:.75rem}
  .rv-prog-wrap{min-width:80px}
  .code-input{font-size:1.6rem;letter-spacing:8px;padding:12px}
}
@media(max-width:360px){
  .code-num{font-size:2rem;letter-spacing:8px}
  .hero-title{font-size:1.75rem}
  .feat-row{gap:12px}
}
@media(min-width:700px){
  .feat-row{gap:32px}
  .hero-sub{font-size:1.05rem}
}
@media(min-width:900px){
  .app{padding:28px 24px}
  .card{padding:28px}
}
</style>
</head>
<body>

<!-- ── Header ──────────────────────────────────────────── -->
<header class="hdr">
  <div class="hdr-logo">DARKGEEK</div>
  <div class="hdr-badge">P2P · ZERO UPLOAD</div>
  <div class="hdr-spacer"></div>
  <div class="conn-dot" id="connDot"></div>
  <div class="conn-lbl" id="connLbl">Connecting...</div>
</header>

<!-- ── Landing / Hero ───────────────────────────────────── -->
<div id="heroPage">
<section class="hero">
  <div class="hero-badge">⚡ WebRTC Direct Transfer</div>

  <h1 class="hero-title">
    <span class="g1">Blazing Fast</span><br>
    <span class="g2">File Sharing</span>
  </h1>

  <p class="hero-sub">
    Transfer files directly between devices — <strong>no upload to server</strong>.<br>
    Up to <strong>5 GB</strong> on WiFi · <strong>500 MB</strong> over Internet.
  </p>

  <div class="mode-grid">
    <div class="mode-card wifi" onclick="pickMode('wifi')">
      <div class="mc-icon">📡</div>
      <div class="mc-title">Local WiFi</div>
      <div class="mc-desc">Ultra-fast LAN transfer. Both devices must be on the same WiFi network.</div>
      <div class="mc-stats">
        <span class="mc-stat">Up to 5 GB</span>
        <span class="mc-stat">~500 MB/s</span>
        <span class="mc-stat">LAN Only</span>
      </div>
      <span class="mc-arrow">→</span>
    </div>

    <div class="mode-card inet" onclick="pickMode('inet')">
      <div class="mc-icon">🌐</div>
      <div class="mc-title">Over Internet</div>
      <div class="mc-desc">Works across different networks worldwide via WebRTC STUN.</div>
      <div class="mc-stats">
        <span class="mc-stat">Up to 500 MB</span>
        <span class="mc-stat">Global</span>
        <span class="mc-stat">Any Network</span>
      </div>
      <span class="mc-arrow">→</span>
    </div>
  </div>

  <div class="feat-row">
    <div class="feat">🔒 No server storage</div>
    <div class="feat">📁 Multiple files</div>
    <div class="feat">👥 Multi-user</div>
    <div class="feat">📦 Auto ZIP</div>
    <div class="feat">📱 QR sharing</div>
  </div>
</section>
</div>

<!-- ── App Shell ─────────────────────────────────────────── -->
<div class="app" id="appPage">

  <div class="breadcrumb">
    <span class="bc-link" onclick="goHome()">⬅ Home</span>
    <span class="bc-sep">/</span>
    <span class="bc-cur" id="bcMode">—</span>
    <div class="mode-indicator" id="modeChip">—</div>
  </div>

  <div class="tabs">
    <button class="tab send-tab on" id="tabSend" onclick="switchTab('send')">📤 Send Files</button>
    <button class="tab recv-tab"    id="tabRecv" onclick="switchTab('recv')">📥 Receive Files</button>
  </div>

  <!-- ═══ SENDER PANEL ══════════════════════════════════ -->
  <div id="sendPanel">

    <!-- File Picker -->
    <div class="card" id="pickerCard">
      <div class="card-hdr">
        <div class="card-hdr-icon">📂</div>
        Select Files to Share
      </div>

      <div class="dz" id="dz">
        <input type="file" id="fileInput" multiple onchange="onFilesSelected(this.files)">
        <span class="dz-emoji">🗂️</span>
        <div class="dz-title">Drop files here or click to browse</div>
        <div class="dz-sub">
          WiFi: <span class="dz-pill">up to 5 GB</span>
          &nbsp; Internet: <span class="dz-pill">up to 500 MB</span>
        </div>
      </div>

      <div class="fl-wrap" id="fileList"></div>

      <div class="act-bar">
        <button class="btn btn-primary" id="btnStartShare" onclick="startSharing()" disabled>
          🚀 Start Sharing
        </button>
        <!-- STOP BUTTON: appears during active transfer -->
        <button class="btn btn-danger" id="stopBtn" onclick="stopTransfer()">
          ⏹ Stop Transfer
        </button>
        <button class="btn btn-ghost" id="btnClear" onclick="clearFileSelection()">🗑 Clear All</button>
        <span class="total-lbl" id="totalLabel"></span>
      </div>
    </div>

    <!-- Share Info (code + QR) -->
    <div class="card hidden" id="shareInfoCard">
      <div class="card-hdr">
        <div class="card-hdr-icon">🔗</div>
        Share with Receivers
      </div>

      <div class="code-display">
        <div>
          <div class="code-lbl">4-DIGIT CODE</div>
          <div class="code-num" id="codeDisplay">----</div>
        </div>
        <div class="code-actions">
          <button class="btn btn-ghost" onclick="copyCode()">📋 Code</button>
          <button class="btn btn-ghost" onclick="copyLink()">🔗 Link</button>
        </div>
      </div>

      <div class="qr-section" id="qrSection"></div>

      <div class="status status-info" style="margin-top:14px">
        <span class="status-icon">ℹ️</span>
        <span id="shareHint">Share the code or QR. Files transfer directly to receivers.</span>
      </div>
    </div>

    <!-- Connected Receivers -->
    <div class="card hidden" id="receiversCard">
      <div class="card-hdr">
        <div class="card-hdr-icon">👥</div>
        <span>Receivers</span>
        <span style="margin-left:8px;font-size:.78rem;color:var(--muted)" id="recvCount">(0 connected)</span>
      </div>
      <div class="rv-list" id="receiverList"></div>
      <div class="wait-box" id="waitBox">
        <div class="wait-spinner"></div>
        Waiting for receivers to connect...
      </div>
    </div>

  </div><!-- /sendPanel -->

  <!-- ═══ RECEIVER PANEL ════════════════════════════════ -->
  <div id="recvPanel" class="hidden">

    <div class="card" id="codeEntryCard">
      <div class="card-hdr">
        <div class="card-hdr-icon">🔑</div>
        Enter Share Code
      </div>
      <div class="code-input-wrap">
        <input class="code-input" type="tel" id="codeInput"
               maxlength="4" placeholder="0000"
               oninput="this.value=this.value.replace(/\D/g,'')"
               onkeydown="if(event.key==='Enter')joinRoom()">
        <button class="btn btn-purple" onclick="joinRoom()" style="min-width:120px">
          Connect →
        </button>
      </div>
      <div class="status status-info" style="margin-top:14px">
        <span class="status-icon">💡</span>
        Enter the 4-digit code shown on the sender's screen.
      </div>
    </div>

    <div class="card hidden" id="recvProgressCard">
      <div class="card-hdr">
        <div class="card-hdr-icon">⬇️</div>
        Receiving Files
      </div>

      <div class="recv-files" id="recvFileList"></div>

      <div class="status status-info" id="recvStatus">
        <span class="status-icon">⏳</span>
        <span>Connecting to sender...</span>
      </div>

      <div class="ov-section hidden" id="ovSection">
        <div class="ov-header">
          <span>Overall Progress</span>
          <span id="ovPct">0%</span>
        </div>
        <div class="ov-outer"><div class="ov-inner" id="ovBar"></div></div>

        <div class="metrics-row">
          <div class="metric-pill" id="speedPill">⚡ — MB/s</div>
          <div class="metric-pill" id="etaPill">⏱ Calculating...</div>
          <div class="metric-pill" id="recvdPill">📦 0 B / 0 B</div>
        </div>
      </div>
    </div>

  </div><!-- /recvPanel -->

</div><!-- /appPage -->

<div id="toastArea"></div>

<footer>
  <div class="footer-brand">THEDARKGEEKDC</div>
  <div>
    © 2025
    <span class="footer-dots">·</span> WebRTC P2P
    <span class="footer-dots">·</span> Zero Upload
    <span class="footer-dots">·</span> No Storage
  </div>
  <div class="footer-dev">
    Developer: <strong><a href="https://github.com/thedarkgeekdc" target="_blank" rel="noopener">@thedarkgeekdc</a></strong>
    &nbsp;(Mr DK CHAUDHARY)
  </div>
</footer>

<!-- ════════════════════════════════════════════════════════
     JAVASCRIPT
═════════════════════════════════════════════════════════ -->
<script>
// ── Constants ────────────────────────────────────────────────────
const SERVER_URL  = window.location.origin;
const CHUNK_SIZE  = 256 * 1024;        // 256 KB WebRTC chunk
const BLOCK_SIZE  = 4  * 1024 * 1024;  // 4 MB disk read block
const MAX_WIFI_B  = 5  * 1024 * 1024 * 1024;  // 5 GB
const MAX_INET_B  = 500 * 1024 * 1024;         // 500 MB

const ICE_CFG = {
  iceServers: [
    { urls: 'stun:stun.l.google.com:19302'  },
    { urls: 'stun:stun1.l.google.com:19302' },
    { urls: 'stun:stun2.l.google.com:19302' },
    { urls: 'stun:stun.cloudflare.com:3478' },
  ]
};

// ── State ────────────────────────────────────────────────────────
let socket, mode = null, currentTab = 'send';
let selFiles = [], roomCode = null;
let peers = {}, peerNum = 0;

// FIX: Abort flag — set to true when Stop is clicked
let transferAborted = false;
let activeTransferCount = 0;  // track how many DC transfers are running

// Receiver state
let rPC = null, senderSid = null;
let rMeta = null, rBufs = {}, rPendHdr = null;
let rTotalRcv = 0, rTotalSz = 0;
let speedTimer = null, lastBytes = 0, lastTs = 0;

// Local IP (for WiFi QR)
let localIP = null, localPort = null;

// ── Init ─────────────────────────────────────────────────────────
window.addEventListener('load', () => {
  fetch('/api/localip').then(r => r.json())
    .then(d => { localIP = d.ip; localPort = d.port; })
    .catch(() => {});

  connectSocket();

  // Deep link: wait for socket connect before joining
  const params = new URLSearchParams(location.search);
  const code   = params.get('code');
  const m      = params.get('mode') || 'inet';
  if (code) {
    // FIX: Listen for connect event instead of using blind timeout
    const tryAutoJoin = () => {
      pickMode(m, false);
      switchTab('recv');
      document.getElementById('codeInput').value = code;
      joinRoom();
    };
    if (socket && socket.connected) {
      setTimeout(tryAutoJoin, 200);
    } else {
      socket.once('connect', () => setTimeout(tryAutoJoin, 200));
    }
  }

  // Drag-and-drop
  const dz = document.getElementById('dz');
  dz.addEventListener('dragover',  e => { e.preventDefault(); dz.classList.add('over'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('over'));
  dz.addEventListener('drop', e => {
    e.preventDefault(); dz.classList.remove('over');
    onFilesSelected(e.dataTransfer.files);
  });
});

// ── Socket.IO ────────────────────────────────────────────────────
function connectSocket() {
  socket = io(SERVER_URL, {
    transports: ['websocket', 'polling'],
    reconnectionAttempts: 10,
    reconnectionDelay: 1000,
  });

  socket.on('connect',    () => { setConnStatus(true);  console.log('[Socket] Connected:', socket.id); });
  socket.on('disconnect', () => { setConnStatus(false); console.log('[Socket] Disconnected'); });

  // Sender events
  socket.on('room_created',    d => onRoomCreated(d.code));
  socket.on('receiver_joined', d => onReceiverJoined(d.receiver_sid));
  socket.on('webrtc_answer',   d => onSenderGotAnswer(d));
  socket.on('receiver_left',   d => onReceiverLeft(d.sid));

  // Receiver events
  socket.on('joined',           d => onJoined(d));
  socket.on('webrtc_offer',     d => onReceiverGotOffer(d));
  socket.on('sender_left',      () => setRecvStatus('err', '❌ Sender disconnected.'));
  socket.on('transfer_stopped', () => {
    setRecvStatus('warn', '⏹ Sender stopped the transfer.');
    stopSpeedMeter();
  });
  socket.on('error',            d => toast(d.msg, 'err'));

  // FIX: Single ICE handler for BOTH sender and receiver roles
  // Previously this was registered twice causing double ICE processing
  socket.on('webrtc_ice', d => onIce(d));
}

function setConnStatus(online) {
  document.getElementById('connDot').classList.toggle('online', online);
  document.getElementById('connLbl').textContent = online ? 'Connected' : 'Offline';
}

// ── Navigation ────────────────────────────────────────────────────
function goHome() {
  // FIX: Close connections BEFORE clearing peers object
  Object.values(peers).forEach(p => {
    try { if (p.dc) p.dc.close(); } catch(e){}
    try { if (p.pc) p.pc.close(); } catch(e){}
  });
  if (rPC) { try { rPC.close(); } catch(e){} rPC = null; }

  // Now clear state
  mode = null; roomCode = null; selFiles = [];
  peers = {}; peerNum = 0;
  transferAborted = false; activeTransferCount = 0;

  document.getElementById('heroPage').style.display = '';
  document.getElementById('appPage').classList.remove('active');
  document.getElementById('fileList').innerHTML = '';
  document.getElementById('totalLabel').textContent = '';
  document.getElementById('btnStartShare').disabled = true;
  document.getElementById('btnStartShare').textContent = '🚀 Start Sharing';
  document.getElementById('fileInput').value = '';
  setStopBtnVisible(false);
  hide('shareInfoCard'); hide('receiversCard');
  document.getElementById('receiverList').innerHTML = '';
  document.getElementById('recvCount').textContent = '(0 connected)';
  show('waitBox');

  history.replaceState({}, '', '/');
}

function pickMode(m, updateURL = true) {
  mode = m;
  document.getElementById('heroPage').style.display = 'none';
  document.getElementById('appPage').classList.add('active');

  const isWifi = m === 'wifi';
  document.getElementById('bcMode').textContent = isWifi ? '📡 Local WiFi' : '🌐 Over Internet';
  const chip = document.getElementById('modeChip');
  chip.textContent = isWifi ? '📡 WiFi Mode' : '🌐 Internet Mode';
  chip.className   = `mode-indicator ${m}`;

  if (updateURL) history.replaceState({}, '', `/?mode=${m}`);
  switchTab('send');
}

function switchTab(tab) {
  currentTab = tab;
  document.getElementById('tabSend').classList.toggle('on', tab === 'send');
  document.getElementById('tabRecv').classList.toggle('on', tab === 'recv');
  document.getElementById('sendPanel').classList.toggle('hidden', tab !== 'send');
  document.getElementById('recvPanel').classList.toggle('hidden', tab !== 'recv');
}

// ── File Selection ────────────────────────────────────────────────
function onFilesSelected(fl) {
  const maxBytes = mode === 'wifi' ? MAX_WIFI_B : MAX_INET_B;
  let added = 0;
  for (const f of fl) {
    if (f.size > maxBytes) {
      toast(`"${f.name}" exceeds the ${mode === 'wifi' ? '5 GB' : '500 MB'} limit.`, 'warn');
      continue;
    }
    // FIX: Skip duplicate files (same name + size)
    const dup = selFiles.some(x => x.name === f.name && x.size === f.size);
    if (dup) { toast(`"${f.name}" already added.`, 'warn'); continue; }
    selFiles.push(f); added++;
  }
  if (added) renderFileList();
}

function clearFileSelection() {
  if (activeTransferCount > 0) {
    toast('Stop the transfer first.', 'warn'); return;
  }
  selFiles = [];
  document.getElementById('fileInput').value = '';
  renderFileList();
}

function removeFile(i) {
  if (activeTransferCount > 0) {
    toast('Stop the transfer first.', 'warn'); return;
  }
  selFiles.splice(i, 1);
  renderFileList();
}

function renderFileList() {
  const total = selFiles.reduce((a, f) => a + f.size, 0);
  document.getElementById('fileList').innerHTML = selFiles.map((f, i) => `
    <div class="fl-item">
      <span class="fl-ico">${fileIcon(f.name)}</span>
      <div class="fl-inf">
        <div class="fl-nm">${esc(f.name)}</div>
        <div class="fl-meta">${formatSize(f.size)} · ${f.type || 'Unknown type'}</div>
      </div>
      <button class="fl-rm" onclick="removeFile(${i})" title="Remove">✕</button>
    </div>`).join('');

  document.getElementById('totalLabel').textContent =
    selFiles.length ? `${selFiles.length} file${selFiles.length > 1 ? 's' : ''} · ${formatSize(total)}` : '';
  document.getElementById('btnStartShare').disabled = selFiles.length === 0;
}

// ── Start / Stop Sharing ──────────────────────────────────────────
function startSharing() {
  if (!selFiles.length) return;
  transferAborted = false;
  const btn = document.getElementById('btnStartShare');
  btn.disabled = true;
  btn.innerHTML = '⏳ Creating room...';
  document.getElementById('btnClear').disabled = true;
  socket.emit('create_room');
}

// FIX: Stop transfer — closes all peer DCs, notifies receivers via server
function stopTransfer() {
  if (!roomCode) return;
  transferAborted = true;
  toast('Transfer stopped.', 'warn');

  // Notify all receivers via server
  socket.emit('transfer_stop', { code: roomCode });

  // Close all data channels + peer connections
  for (const sid in peers) {
    const p = peers[sid];
    try { if (p.dc) p.dc.close(); } catch(e){}
    try { if (p.pc) p.pc.close(); } catch(e){}
    setRvStatus(sid, 'lost', '⏹ Transfer stopped');
  }
  peers = {}; peerNum = 0;
  activeTransferCount = 0;

  setStopBtnVisible(false);
  const btn = document.getElementById('btnStartShare');
  btn.disabled = false;
  btn.innerHTML = '🚀 Start Again';
  document.getElementById('btnClear').disabled = false;
}

function setStopBtnVisible(show) {
  const btn = document.getElementById('stopBtn');
  if (show) btn.classList.add('active');
  else      btn.classList.remove('active');
}

function onRoomCreated(code) {
  roomCode = code;
  document.getElementById('codeDisplay').textContent = code;
  document.getElementById('btnStartShare').innerHTML = '✅ Room Active';

  // Build QR section
  const qrSec = document.getElementById('qrSection');
  qrSec.innerHTML = '';

  if (mode === 'wifi' && localIP) {
    const wifiURL = `http://${localIP}:${localPort}/?code=${code}&mode=wifi`;
    qrSec.innerHTML += qrBoxHTML(wifiURL, '📡 Same WiFi', 'Scan on same network');
    document.getElementById('shareHint').textContent =
      'Scan WiFi QR on same network for max speed — or share the 4-digit code.';
  }

  const inetURL = `${SERVER_URL}/?code=${code}&mode=inet`;
  qrSec.innerHTML += qrBoxHTML(inetURL, '🌐 Internet', 'Works from any network');

  show('shareInfoCard');
  show('receiversCard');
}

function qrBoxHTML(url, label, sub) {
  return `
    <div class="qr-box">
      <img src="/qr?d=${encodeURIComponent(url)}" alt="QR" loading="lazy">
      <div class="qr-lbl"><strong>${label}</strong><br>${sub}</div>
    </div>`;
}

// ── Sender: Receiver Joined ───────────────────────────────────────
async function onReceiverJoined(sid) {
  if (transferAborted) return;
  peerNum++;
  addReceiverRow(sid, peerNum);
  hide('waitBox');
  updateRecvCount();
  await initSenderPeer(sid, peerNum);
}

async function initSenderPeer(sid, num) {
  const pc = new RTCPeerConnection(ICE_CFG);
  const dc = pc.createDataChannel('share', { ordered: true });
  peers[sid] = { pc, dc, bytesSent: 0, totalBytes: 0, num };

  dc.bufferedAmountLowThreshold = CHUNK_SIZE * 4;

  dc.onopen = () => {
    if (transferAborted) { try { dc.close(); } catch(e){} return; }
    setRvStatus(sid, 'active', 'Transferring...');
    setStopBtnVisible(true);
    activeTransferCount++;
    sendAllFiles(sid).catch(e => {
      console.error('[sendAllFiles]', e);
      setRvStatus(sid, 'lost', 'Transfer error');
    }).finally(() => {
      activeTransferCount = Math.max(0, activeTransferCount - 1);
      if (activeTransferCount === 0) setStopBtnVisible(false);
    });
  };

  dc.onclose = () => {
    console.log('[DC] Closed for', sid);
    activeTransferCount = Math.max(0, activeTransferCount - 1);
    if (activeTransferCount === 0) setStopBtnVisible(false);
  };
  dc.onerror = e => {
    setRvStatus(sid, 'lost', 'Channel error');
    console.error('[DC error]', e);
  };

  pc.onicecandidate = e => {
    if (e.candidate) socket.emit('webrtc_ice', { to: sid, candidate: e.candidate });
  };
  pc.onconnectionstatechange = () => {
    const st = pc.connectionState;
    if (st === 'failed' || st === 'disconnected') {
      setRvStatus(sid, 'lost', 'Connection lost');
    }
  };

  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    socket.emit('webrtc_offer', { to: sid, offer: pc.localDescription });
  } catch(e) {
    console.error('[initSenderPeer] offer error:', e);
    setRvStatus(sid, 'lost', 'WebRTC error');
  }
}

async function onSenderGotAnswer(data) {
  const peer = peers[data.from];
  if (!peer) return;
  try {
    await peer.pc.setRemoteDescription(new RTCSessionDescription(data.answer));
  } catch(e) { console.error('[setRemoteDesc answer]', e); }
}

function onReceiverLeft(sid) {
  if (peers[sid]) {
    setRvStatus(sid, 'lost', 'Disconnected');
    try { peers[sid].dc.close(); } catch(e){}
    try { peers[sid].pc.close(); } catch(e){}
    delete peers[sid];
    updateRecvCount();
  }
}

// ── File Send ─────────────────────────────────────────────────────
async function sendAllFiles(sid) {
  const peer = peers[sid];
  if (!peer) return;
  const dc    = peer.dc;
  const total = selFiles.reduce((a, f) => a + f.size, 0);
  peer.totalBytes = total;

  // Send metadata
  safeSend(dc, JSON.stringify({
    type: 'meta', totalSize: total,
    files: selFiles.map((f, i) => ({
      index: i, name: f.name, size: f.size,
      mime: f.type || 'application/octet-stream',
    })),
  }));

  for (let i = 0; i < selFiles.length; i++) {
    // FIX: Check abort flag before each file
    if (transferAborted || !peers[sid]) return;
    await sendOneFile(dc, selFiles[i], i, sid);
  }

  if (!transferAborted && peers[sid]) {
    safeSend(dc, JSON.stringify({ type: 'all_done' }));
    setRvStatus(sid, 'done', '✅ Transfer complete!');
    setRvProgress(sid, 100);
  }
}

async function sendOneFile(dc, file, idx, sid) {
  safeSend(dc, JSON.stringify({
    type: 'file_start', idx, name: file.name, size: file.size
  }));

  let offset = 0;
  while (offset < file.size) {
    // FIX: Check abort at each block boundary
    if (transferAborted || !peers[sid] || dc.readyState !== 'open') return;

    const blockEnd = Math.min(offset + BLOCK_SIZE, file.size);
    let buf;
    try {
      buf = await file.slice(offset, blockEnd).arrayBuffer();
    } catch(e) {
      console.error('[read file]', e);
      setRvStatus(sid, 'lost', 'File read error');
      return;
    }

    let blkOff = 0;
    while (blkOff < buf.byteLength) {
      // FIX: Check abort inside chunk loop
      if (transferAborted || !peers[sid] || dc.readyState !== 'open') return;

      // Back-pressure
      if (dc.bufferedAmount > CHUNK_SIZE * 8) {
        await new Promise((res, rej) => {
          const timeout = setTimeout(() => {
            dc.onbufferedamountlow = null;
            // Check if we should abort rather than wait
            if (transferAborted || !peers[sid]) rej(new Error('aborted'));
            else res();
          }, 10000); // 10s timeout fallback
          dc.bufferedAmountLowThreshold = CHUNK_SIZE * 2;
          dc.onbufferedamountlow = () => {
            clearTimeout(timeout);
            dc.onbufferedamountlow = null;
            res();
          };
        }).catch(() => { transferAborted = true; });
        if (transferAborted) return;
      }

      const end   = Math.min(blkOff + CHUNK_SIZE, buf.byteLength);
      const chunk = buf.slice(blkOff, end);

      // FIX: Wrap dc.send in try-catch — throws if channel closed mid-transfer
      if (!safeSend(dc, JSON.stringify({
        type: 'chunk', idx, offset: offset + blkOff, sz: chunk.byteLength
      }))) return;
      if (!safeSend(dc, chunk)) return;

      blkOff += chunk.byteLength;
      if (peers[sid]) {
        peers[sid].bytesSent += chunk.byteLength;
        const pct = Math.round(peers[sid].bytesSent / peers[sid].totalBytes * 100);
        setRvProgress(sid, pct);
      }
    }
    offset = blockEnd;
  }
  safeSend(dc, JSON.stringify({ type: 'file_done', idx }));
}

// FIX: Safe dc.send wrapper — returns false if channel closed
function safeSend(dc, data) {
  try {
    if (dc.readyState !== 'open') return false;
    dc.send(data);
    return true;
  } catch(e) {
    console.warn('[safeSend] failed:', e.message);
    return false;
  }
}

// ── Receiver: Join Room ───────────────────────────────────────────
function joinRoom() {
  const code = document.getElementById('codeInput').value.trim();
  if (!/^\d{4}$/.test(code)) { toast('Please enter a valid 4-digit code.', 'warn'); return; }
  if (!socket.connected) { toast('Not connected to server. Please wait.', 'err'); return; }
  socket.emit('join_room_request', { code });
}

function onJoined(data) {
  roomCode = data.code; senderSid = data.sender_sid;
  hide('codeEntryCard');
  show('recvProgressCard');
  setRecvStatus('info', '⏳ Connecting to sender...');
}

// ── Receiver: WebRTC ──────────────────────────────────────────────
async function onReceiverGotOffer(data) {
  senderSid = data.from;
  rPC = new RTCPeerConnection(ICE_CFG);

  rPC.onicecandidate = e => {
    if (e.candidate) socket.emit('webrtc_ice', { to: senderSid, candidate: e.candidate });
  };
  rPC.ondatachannel = e => setupReceiverDC(e.channel);
  rPC.onconnectionstatechange = () => {
    const st = rPC.connectionState;
    if (st === 'connected') setRecvStatus('ok', '✅ Connected! Receiving files...');
    if (st === 'failed')    setRecvStatus('err', '❌ Connection failed. Please try again.');
    if (st === 'disconnected') setRecvStatus('warn', '⚠️ Connection dropped.');
  };

  try {
    await rPC.setRemoteDescription(new RTCSessionDescription(data.offer));
    const answer = await rPC.createAnswer();
    await rPC.setLocalDescription(answer);
    socket.emit('webrtc_answer', { to: senderSid, answer: rPC.localDescription });
  } catch(e) {
    console.error('[onReceiverGotOffer]', e);
    setRecvStatus('err', '❌ WebRTC setup failed.');
  }
}

function setupReceiverDC(dc) {
  dc.binaryType = 'arraybuffer';
  dc.onmessage  = e => {
    if (typeof e.data === 'string') handleRecvControl(JSON.parse(e.data));
    else                             handleRecvBinary(e.data);
  };
  dc.onerror = e => setRecvStatus('err', '❌ Data channel error.');
  dc.onclose = () => console.log('[DC] Receiver channel closed');
}

function handleRecvControl(msg) {
  switch (msg.type) {
    case 'meta':
      rMeta = msg; rTotalSz = msg.totalSize;
      rTotalRcv = 0; rBufs = {};
      msg.files.forEach(f => {
        rBufs[f.index] = { meta: f, chunks: [], received: 0 };
      });
      renderRecvFileList(msg.files);
      show('ovSection');
      startSpeedMeter();
      break;
    case 'chunk':
      rPendHdr = msg;
      break;
    case 'file_done':
      markFileDone(msg.idx);
      break;
    case 'all_done':
      finalizeDownload();
      break;
    case 'file_start':
      // Receiver acknowledges file start (no action needed currently)
      break;
  }
}

function handleRecvBinary(data) {
  if (!rPendHdr) return;
  const { idx } = rPendHdr;
  const buf     = rBufs[idx];
  if (!buf) { rPendHdr = null; return; }

  buf.chunks.push(data);
  buf.received += data.byteLength;
  rTotalRcv    += data.byteLength;
  rPendHdr      = null;

  const pct = Math.round(rTotalRcv / rTotalSz * 100);
  document.getElementById('ovPct').textContent = pct + '%';
  document.getElementById('ovBar').style.width = pct + '%';

  const fpct = Math.round(buf.received / buf.meta.size * 100);
  const el   = document.getElementById(`rfbar-${idx}`);
  const pcts = document.getElementById(`rfpct-${idx}`);
  if (el)   el.style.width   = fpct + '%';
  if (pcts) pcts.textContent = formatSize(buf.received) + ' / ' + formatSize(buf.meta.size);

  document.getElementById('recvdPill').textContent =
    `📦 ${formatSize(rTotalRcv)} / ${formatSize(rTotalSz)}`;
}

function markFileDone(idx) {
  const el = document.getElementById(`rfbar-${idx}`);
  if (el) el.style.width = '100%';
  const pcts = document.getElementById(`rfpct-${idx}`);
  if (pcts) pcts.textContent = '✅ Done';
}

async function finalizeDownload() {
  stopSpeedMeter();
  setRecvStatus('ok', '✅ Transfer complete! Preparing download...');

  const all = Object.values(rBufs);
  try {
    if (all.length === 1) {
      const f = all[0];
      downloadBlob(new Blob(f.chunks, { type: f.meta.mime }), f.meta.name);
    } else {
      setRecvStatus('info', '📦 Creating ZIP archive...');
      const zip = new JSZip();
      all.forEach(f => zip.file(f.meta.name, new Blob(f.chunks)));
      const blob = await zip.generateAsync(
        { type: 'blob', compression: 'DEFLATE', compressionOptions: { level: 1 } },
        m => setRecvStatus('info', `📦 ZIP: ${m.percent.toFixed(0)}%`)
      );
      downloadBlob(blob, 'shared_files.zip');
    }
    setRecvStatus('ok', '🎉 Download started! Check your Downloads folder.');
  } catch(e) {
    console.error('[finalizeDownload]', e);
    setRecvStatus('err', '❌ Failed to create download. Try again.');
  }
}

function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a   = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 15000);
}

// ── ICE relay ─────────────────────────────────────────────────────
// FIX: Single unified ICE handler — was registered twice before
async function onIce(data) {
  const { candidate, from } = data;
  // Try sender peer map first
  if (peers[from]) {
    try { await peers[from].pc.addIceCandidate(new RTCIceCandidate(candidate)); } catch(e){}
    return;
  }
  // Then try receiver PC
  if (rPC && from === senderSid) {
    try { await rPC.addIceCandidate(new RTCIceCandidate(candidate)); } catch(e){}
  }
}

// ── Sender UI helpers ─────────────────────────────────────────────
function addReceiverRow(sid, num) {
  const list = document.getElementById('receiverList');
  const div  = document.createElement('div');
  div.className = 'rv-item';
  div.id = `rv-${sid}`;
  div.innerHTML = `
    <div class="rv-avatar">${num}</div>
    <div class="rv-info">
      <div class="rv-name">Receiver ${num}</div>
      <div class="rv-status" id="rvst-${sid}">Connecting...</div>
    </div>
    <div class="rv-dot connecting" id="rvd-${sid}"></div>
    <div class="rv-prog-wrap">
      <div class="prog-bar-outer"><div class="prog-bar-inner" id="rvp-${sid}"></div></div>
      <div class="prog-pct" id="rvpct-${sid}">0%</div>
    </div>`;
  list.appendChild(div);
}

function setRvStatus(sid, state, text) {
  const dot = document.getElementById(`rvd-${sid}`);
  const st  = document.getElementById(`rvst-${sid}`);
  if (dot) dot.className = `rv-dot ${state}`;
  if (st)  st.textContent = text;
}

function setRvProgress(sid, pct) {
  const pb   = document.getElementById(`rvp-${sid}`);
  const pct2 = document.getElementById(`rvpct-${sid}`);
  if (pb)   pb.style.width   = pct + '%';
  if (pct2) pct2.textContent = pct + '%';
}

function updateRecvCount() {
  const n = Object.keys(peers).length;
  document.getElementById('recvCount').textContent = `(${n} connected)`;
}

// ── Receiver UI helpers ───────────────────────────────────────────
function renderRecvFileList(files) {
  document.getElementById('recvFileList').innerHTML = files.map(f => `
    <div class="rf-item">
      <div class="rf-header">
        <span style="font-size:1.5rem">${fileIcon(f.name)}</span>
        <div class="rf-info">
          <div class="rf-nm">${esc(f.name)}</div>
          <div class="rf-meta" id="rfpct-${f.index}">0 B / ${formatSize(f.size)}</div>
        </div>
      </div>
      <div class="rf-prog-bar"><div class="rf-prog-fill" id="rfbar-${f.index}"></div></div>
    </div>`).join('');
}

function setRecvStatus(type, msg) {
  const el = document.getElementById('recvStatus');
  el.className = `status status-${
    type === 'ok'   ? 'ok'   :
    type === 'err'  ? 'err'  :
    type === 'warn' ? 'warn' : 'info'
  }`;
  const icon = type === 'ok' ? '✅' : type === 'err' ? '❌' : type === 'warn' ? '⚠️' : 'ℹ️';
  el.innerHTML = `<span class="status-icon">${icon}</span><span>${msg}</span>`;
}

// ── Speed Meter ───────────────────────────────────────────────────
function startSpeedMeter() {
  lastBytes = 0; lastTs = Date.now();
  speedTimer = setInterval(() => {
    const now = Date.now();
    const dt  = (now - lastTs) / 1000;
    if (dt <= 0) return;
    const bps = (rTotalRcv - lastBytes) / dt;
    lastBytes = rTotalRcv; lastTs = now;
    document.getElementById('speedPill').textContent = `⚡ ${formatSpeed(bps)}`;
    if (bps > 0 && rTotalSz > rTotalRcv) {
      const eta = (rTotalSz - rTotalRcv) / bps;
      document.getElementById('etaPill').textContent = `⏱ ${formatTime(eta)}`;
    }
  }, 1000);
}
function stopSpeedMeter() {
  if (speedTimer) { clearInterval(speedTimer); speedTimer = null; }
}

// ── Copy helpers ──────────────────────────────────────────────────
function copyCode() {
  navigator.clipboard?.writeText(roomCode);
  toast('Code copied: ' + roomCode);
}
function copyLink() {
  const url = `${SERVER_URL}/?code=${roomCode}&mode=${mode}`;
  navigator.clipboard?.writeText(url);
  toast('Link copied!');
}

// ── Toast ─────────────────────────────────────────────────────────
function toast(msg, type = 'ok') {
  const area = document.getElementById('toastArea');
  const el   = document.createElement('div');
  el.className = 'toast-item';
  el.style.borderLeftColor =
    type === 'err'  ? 'var(--red)'   :
    type === 'warn' ? 'var(--amber)' : 'var(--cyan)';
  el.textContent = msg;
  area.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity .3s';
    setTimeout(() => el.remove(), 300);
  }, 3000);
}

function show(id) { document.getElementById(id)?.classList.remove('hidden'); }
function hide(id) { document.getElementById(id)?.classList.add('hidden'); }

// ── Formatters ────────────────────────────────────────────────────
function formatSize(b) {
  if (b < 1024)       return b + ' B';
  if (b < 1048576)    return (b / 1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1073741824).toFixed(2) + ' GB';
}
function formatSpeed(bps) {
  if (bps < 1024)    return bps.toFixed(0) + ' B/s';
  if (bps < 1048576) return (bps / 1024).toFixed(1) + ' KB/s';
  return (bps / 1048576).toFixed(1) + ' MB/s';
}
function formatTime(s) {
  if (s < 60)   return s.toFixed(0) + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + Math.floor(s % 60) + 's';
  return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
}

function fileIcon(name) {
  const ext = name.split('.').pop().toLowerCase();
  return ({
    pdf:'📄', doc:'📝', docx:'📝', txt:'📝', rtf:'📝',
    jpg:'🖼', jpeg:'🖼', png:'🖼', gif:'🖼', webp:'🖼', svg:'🖼', bmp:'🖼',
    mp4:'🎬', mkv:'🎬', avi:'🎬', mov:'🎬', webm:'🎬', flv:'🎬',
    mp3:'🎵', wav:'🎵', flac:'🎵', m4a:'🎵', aac:'🎵', ogg:'🎵',
    zip:'📦', rar:'📦', '7z':'📦', tar:'📦', gz:'📦',
    apk:'📱', ipa:'📱',
    exe:'⚙',  msi:'⚙',  dmg:'⚙',
    py:'🐍',  js:'📜',  ts:'📜', html:'🌐', css:'🎨',
    json:'🗒', xml:'🗒', csv:'📊',
    xlsx:'📊', xls:'📊', pptx:'📊', ppt:'📊',
    iso:'💿', img:'💿',
  }[ext] || '📁');
}

function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
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
║   ⚡  THEDARKGEEKDC — P2P File Share  v3.0          ║
╠══════════════════════════════════════════════════════╣
║  📶  Local :  http://{local_ip}:{PORT:<5}               ║
║  🌐  Public:  Deploy on Railway for internet use    ║
║  🚀  Mode  :  WebRTC Direct · Zero Upload           ║
║  👤  Dev   :  @thedarkgeekdc (Mr DK CHAUDHARY)      ║
╚══════════════════════════════════════════════════════╝
    """)
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
