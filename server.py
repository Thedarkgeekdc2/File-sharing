"""
ShareAnywhere — P2P File Share + Chat
Railway-ready | Zero Upload | WebRTC Direct | Multi-User
Dev: Mr. D.K. Chaudhary (@THEDARKGEEKDC)
"""

# ── B1: monkey_patch MUST be before every other import ────────────
import eventlet
eventlet.monkey_patch()
# ─────────────────────────────────────────────────────────────────

import os, io, time, random, socket, ipaddress, threading

import qrcode
from flask import Flask, request, send_file, send_from_directory, jsonify
from flask_socketio import SocketIO, emit, join_room

# ─────────────────────────────────────────────────────────────────
#  APP
# ─────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "shareanywhere_2026_dk")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=20,
    max_http_buffer_size=1024 * 1024,
)

PORT = int(os.environ.get("PORT", 5683))

# ─────────────────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────────────────
rooms            = {}   # code → {sender, receivers, created_at}
pending_requests = {}   # req_id → {code, sender_sid, receiver_sid, created_at}
rooms_lock       = threading.Lock()
req_lock         = threading.Lock()


# ─────────────────────────────────────────────────────────────────
#  HELPERS
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


def is_private(host: str) -> bool:
    h = (host or "").strip().lower().split(":")[0]
    if h in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except Exception:
        return False


def cleanup_pending_for(sid: str):
    with req_lock:
        for rid, req in list(pending_requests.items()):
            if req["sender_sid"] == sid:
                socketio.emit("request_canceled", {"request_id": rid}, to=req["receiver_sid"])
                pending_requests.pop(rid, None)
            elif req["receiver_sid"] == sid:
                pending_requests.pop(rid, None)


def _cleanup_loop():
    while True:
        eventlet.sleep(300)
        cutoff = time.time() - 7200
        with rooms_lock:
            for code in [c for c, r in rooms.items() if r["created_at"] < cutoff]:
                socketio.emit("sender_left", {}, to=code)
                rooms.pop(code, None)
                print(f"[cleanup] room {code} expired")
        with req_lock:
            cutoff2 = time.time() - 900
            for rid in [r for r, v in pending_requests.items() if v["created_at"] < cutoff2]:
                pending_requests.pop(rid, None)


eventlet.spawn(_cleanup_loop)


# ─────────────────────────────────────────────────────────────────
#  HTTP ROUTES
# ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/localip")
def api_localip():
    return jsonify({"ip": get_local_ip(), "port": PORT})


@app.route("/api/network")
def api_network():
    host = request.host.split(":")[0]
    mode = "wifi" if is_private(host) else "inet"
    return jsonify({
        "mode":  mode,
        "label": "Local WiFi" if mode == "wifi" else "Internet",
        "local_ip": get_local_ip(),
        "port": PORT,
    })


@app.route("/qr")
def gen_qr():
    data = request.args.get("d", request.url_root)
    size = max(4, min(20, int(request.args.get("s", 9))))
    qr   = qrcode.QRCode(version=None,
                          error_correction=qrcode.constants.ERROR_CORRECT_M,
                          box_size=size, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    # Use RGB tuples — works on all qrcode versions
    img = qr.make_image(fill_color=(0, 200, 240), back_color=(7, 7, 26))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


# ─────────────────────────────────────────────────────────────────
#  SOCKET EVENTS
# ─────────────────────────────────────────────────────────────────

@socketio.on("create_room")
def on_create_room():
    sid = request.sid
    with rooms_lock:
        for _ in range(300):
            code = str(random.randint(1000, 9999))
            if code not in rooms:
                break
        else:
            emit("error", {"msg": "Server busy. Try again."})
            return
        rooms[code] = {"sender": sid, "receivers": set(), "created_at": time.time()}
    join_room(code)
    emit("room_created", {"code": code})
    print(f"[+] Room {code} by {sid[:8]}")


@socketio.on("join_room_request")
def on_join_request(data):
    code = str((data or {}).get("code", "")).strip()
    sid  = request.sid
    with rooms_lock:
        if code not in rooms:
            emit("error", {"msg": "Invalid code or room expired."})
            return
        sender_sid = rooms[code]["sender"]

    req_id = f"{sid}:{int(time.time()*1000)}"
    with req_lock:
        pending_requests[req_id] = {
            "code": code, "sender_sid": sender_sid,
            "receiver_sid": sid, "created_at": time.time(),
        }

    socketio.emit("incoming_request", {"request_id": req_id, "receiver_sid": sid}, to=sender_sid)
    emit("waiting_for_approval", {"request_id": req_id})
    print(f"[req] {sid[:8]} → room {code}")


@socketio.on("request_decision")
def on_decision(data):
    rid      = (data or {}).get("request_id")
    decision = (data or {}).get("decision")
    sid      = request.sid

    with req_lock:
        req = pending_requests.pop(rid, None)
    if not req or req["sender_sid"] != sid:
        return

    receiver_sid = req["receiver_sid"]
    code         = req["code"]

    if decision == "accept":
        with rooms_lock:
            room = rooms.get(code)
            if not room:
                socketio.emit("request_canceled", {"request_id": rid}, to=receiver_sid)
                return
            room["receivers"].add(receiver_sid)

        # Add receiver to SocketIO room
        socketio.server.enter_room(receiver_sid, code,
                                   namespace=socketio.namespace)

        socketio.emit("request_accepted",
                      {"request_id": rid, "code": code, "sender_sid": sid},
                      to=receiver_sid)
        socketio.emit("receiver_joined",
                      {"receiver_sid": receiver_sid},
                      to=sid)
        print(f"[accept] {sid[:8]} accepted {receiver_sid[:8]} in {code}")
    else:
        socketio.emit("request_rejected", {"request_id": rid}, to=receiver_sid)
        print(f"[reject] {sid[:8]} rejected {receiver_sid[:8]}")


@socketio.on("webrtc_offer")
def on_offer(data):
    to = (data or {}).get("to")
    if to:
        socketio.emit("webrtc_offer", {"offer": data["offer"], "from": request.sid}, to=to)


@socketio.on("webrtc_answer")
def on_answer(data):
    to = (data or {}).get("to")
    if to:
        socketio.emit("webrtc_answer", {"answer": data["answer"], "from": request.sid}, to=to)


@socketio.on("webrtc_ice")
def on_ice(data):
    to = (data or {}).get("to")
    if to:
        socketio.emit("webrtc_ice",
                      {"candidate": data["candidate"], "from": request.sid}, to=to)


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    cleanup_pending_for(sid)
    with rooms_lock:
        for code, room in list(rooms.items()):
            if sid == room["sender"]:
                socketio.emit("sender_left", {}, to=code)
                rooms.pop(code, None)
                print(f"[-] Sender left → room {code} closed")
                break
            elif sid in room["receivers"]:
                room["receivers"].discard(sid)
                socketio.emit("receiver_left", {"sid": sid}, to=room["sender"])
                print(f"[-] Receiver {sid[:8]} left room {code}")
                break


# ─────────────────────────────────────────────────────────────────
#  STARTUP
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    local_ip = get_local_ip()
    print(f"""
╔══════════════════════════════════════════════════════╗
║    ShareAnywhere — P2P File Share + Chat            ║
╠══════════════════════════════════════════════════════╣
║  Local  : http://{local_ip}:{PORT:<5}                      ║
║  Mode   : WebRTC Direct · Zero Upload               ║
║  Dev    : Mr DK CHAUDHARY (@THEDARKGEEKDC)          ║
╚══════════════════════════════════════════════════════╝
""")
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
