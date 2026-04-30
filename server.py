import os
import io
import time
import random
import socket
import threading
import ipaddress

import eventlet

eventlet.monkey_patch()

import qrcode
from flask import Flask, request, send_file, send_from_directory, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "shareanywhere_2026")

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

# ────────────────────────────────────────────────────────────────
# Name / room state
# ────────────────────────────────────────────────────────────────
rooms = {}
pending_requests = {}
sid_name = {}
used_names = set()
rooms_lock = threading.Lock()
req_lock = threading.Lock()
name_lock = threading.Lock()


def clean_name(value: str) -> str:
    value = (value or "").strip()
    value = "_".join(value.split())
    value = "".join(ch for ch in value if ch.isalnum() or ch in "_-.@")
    return value[:32] or "user"


def unique_name(base: str) -> str:
    base = clean_name(base)
    with name_lock:
        if base not in used_names:
            used_names.add(base)
            return base
        i = 1
        while f"{base}_a{i}" in used_names:
            i += 1
        name = f"{base}_a{i}"
        used_names.add(name)
        return name


def release_name(sid: str) -> None:
    with name_lock:
        name = sid_name.pop(sid, None)
        if name:
            used_names.discard(name)


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def is_private_host(host: str) -> bool:
    host = (host or "").strip().lower().split(":")[0]
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except Exception:
        return False


def default_network_mode(host: str) -> str:
    return "wifi" if is_private_host(host) else "inet"


def cleanup_loop():
    while True:
        eventlet.sleep(300)
        cutoff = time.time() - 7200
        with rooms_lock:
            expired_codes = [code for code, room in rooms.items() if room["created_at"] < cutoff]
            for code in expired_codes:
                room = rooms.pop(code, None)
                if not room:
                    continue
                try:
                    socketio.emit("sender_left", {}, to=code)
                except Exception:
                    pass
                print(f"[cleanup] room {code} expired")
        with req_lock:
            expired_reqs = [rid for rid, req in pending_requests.items() if req["created_at"] < time.time() - 900]
            for rid in expired_reqs:
                pending_requests.pop(rid, None)


eventlet.spawn(cleanup_loop)


# ────────────────────────────────────────────────────────────────
# HTTP routes
# ────────────────────────────────────────────────────────────────
@app.route("/")
@app.route("/connected")
@app.route("/share")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/localip")
def api_localip():
    return jsonify({"ip": get_local_ip(), "port": PORT})


@app.route("/api/network")
def api_network():
    host = request.host.split(":")[0]
    mode = default_network_mode(host)
    return jsonify({
        "host": host,
        "local_ip": get_local_ip(),
        "port": PORT,
        "default_mode": mode,
        "network_label": "Local WiFi" if mode == "wifi" else "Internet",
    })


@app.route("/qr")
def gen_qr():
    data = request.args.get("d", request.url_root)
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


# ────────────────────────────────────────────────────────────────
# Socket helpers
# ────────────────────────────────────────────────────────────────
def sender_of(code):
    room = rooms.get(code)
    if room:
        return room["sender"]
    return None


def cleanup_pending_for_sid(sid: str):
    with req_lock:
        for rid, req in list(pending_requests.items()):
            if req["sender_sid"] == sid:
                socketio.emit("request_canceled", {"request_id": rid}, to=req["receiver_sid"])
                pending_requests.pop(rid, None)
            elif req["receiver_sid"] == sid:
                pending_requests.pop(rid, None)


# ────────────────────────────────────────────────────────────────
# Socket events
# ────────────────────────────────────────────────────────────────
@socketio.on("register_name")
def on_register_name(data):
    sid = request.sid
    raw = clean_name((data or {}).get("name", "user"))

    # Release any previous name tied to this sid
    with name_lock:
        prev = sid_name.get(sid)
        if prev:
            used_names.discard(prev)
        name = unique_name(raw)
        sid_name[sid] = name

    emit("name_ok", {"name": name})
    print(f"[name] {sid[:8]} -> {name}")


@socketio.on("create_room")
def on_create_room():
    sid = request.sid
    with rooms_lock:
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
    emit(
        "room_created",
        {
            "code": code,
            "sender_name": sid_name.get(sid, sid[:8]),
        },
    )
    print(f"[room] {code} created by {sid[:8]}")


@socketio.on("join_room_request")
def on_join_room_request(data):
    code = str((data or {}).get("code", "")).strip()
    sid = request.sid
    receiver_name = sid_name.get(sid, sid[:8])

    with rooms_lock:
        if code not in rooms:
            emit("error", {"msg": "Invalid code or room has expired."})
            return
        room = rooms[code]
        sender_sid = room["sender"]

    req_id = f"{sid}:{int(time.time() * 1000)}"
    with req_lock:
        pending_requests[req_id] = {
            "code": code,
            "sender_sid": sender_sid,
            "receiver_sid": sid,
            "receiver_name": receiver_name,
            "created_at": time.time(),
        }

    socketio.emit(
        "incoming_request",
        {
            "request_id": req_id,
            "code": code,
            "receiver_sid": sid,
            "receiver_name": receiver_name,
        },
        to=sender_sid,
    )
    emit(
        "waiting_for_approval",
        {"request_id": req_id, "sender_name": sid_name.get(sender_sid, sender_sid[:8])},
    )
    print(f"[request] {receiver_name} -> room {code}")


@socketio.on("request_decision")
def on_request_decision(data):
    rid = (data or {}).get("request_id")
    decision = (data or {}).get("decision")
    sid = request.sid

    with req_lock:
        req = pending_requests.pop(rid, None)

    if not req:
        emit("error", {"msg": "Request expired."})
        return

    if req["sender_sid"] != sid:
        emit("error", {"msg": "Not allowed."})
        return

    receiver_sid = req["receiver_sid"]
    code = req["code"]
    sender_name = sid_name.get(sid, sid[:8])
    receiver_name = req["receiver_name"]

    if decision == "accept":
        with rooms_lock:
            room = rooms.get(code)
            if not room:
                socketio.emit("request_canceled", {"request_id": rid}, to=receiver_sid)
                return
            room["receivers"].add(receiver_sid)

        join_room(code, sid=receiver_sid)
        socketio.emit(
            "request_accepted",
            {
                "request_id": rid,
                "code": code,
                "sender_sid": sid,
                "sender_name": sender_name,
                "receiver_name": receiver_name,
            },
            to=receiver_sid,
        )
        socketio.emit(
            "receiver_joined",
            {
                "receiver_sid": receiver_sid,
                "receiver_name": receiver_name,
                "sender_name": sender_name,
            },
            to=sid,
        )
        print(f"[accept] {sender_name} accepted {receiver_name} in {code}")
    else:
        socketio.emit(
            "request_rejected",
            {"request_id": rid, "code": code, "sender_name": sender_name},
            to=receiver_sid,
        )
        print(f"[reject] {sender_name} rejected {receiver_name} in {code}")


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
        socketio.emit(
            "webrtc_ice",
            {"candidate": data["candidate"], "from": request.sid},
            to=to,
        )


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid

    cleanup_pending_for_sid(sid)

    with rooms_lock:
        for code, room in list(rooms.items()):
            if sid == room["sender"]:
                socketio.emit("sender_left", {}, to=code)
                for rsid in list(room["receivers"]):
                    try:
                        leave_room(code, sid=rsid)
                    except Exception:
                        pass
                rooms.pop(code, None)
                print(f"[-] sender left -> room {code} closed")
                break
            elif sid in room["receivers"]:
                room["receivers"].discard(sid)
                try:
                    leave_room(code, sid=sid)
                except Exception:
                    pass
                socketio.emit("receiver_left", {"sid": sid}, to=room["sender"])
                print(f"[-] receiver {sid[:8]} left room {code}")
                break

    release_name(sid)


# ────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        owner = input("Enter your name before starting the server: ").strip()
    except EOFError:
        owner = os.environ.get("SERVER_OWNER_NAME", "Mr_DK")
    owner = clean_name(owner or "Mr_DK")
    print(f"Server owner: {owner}")

    local_ip = get_local_ip()
    default_mode = default_network_mode(local_ip)
    print(
        f"""
╔══════════════════════════════════════════════════════╗
║   ShareAnywhere — P2P File Share + Chat             ║
╠══════════════════════════════════════════════════════╣
║  Local : http://{local_ip}:{PORT:<5}                         ║
║  Net   : {"Local WiFi" if default_mode == "wifi" else "Internet":<44}║
║  Mode  : WebRTC Direct · Zero Upload                ║
║  Owner : {owner:<44}║
╚══════════════════════════════════════════════════════╝
"""
    )
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
