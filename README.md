# ⚡ P2P File Share — THEDARKGEEKDC v2.0

> Zero Upload · WebRTC Direct · Multi-User · Railway Ready

---

## How It Works

```
┌─────────────────────────────────────────────┐
│           Railway Server                    │
│    (Signaling only — NO file data)         │
│   WebSocket: offer / answer / ICE only     │
└────────────┬──────────────┬────────────────┘
             │              │
     Sender Browser ──────► Receiver Browser
          [WebRTC DataChannel — Direct P2P]
          
  Same WiFi → LAN path → ~500 MB/s
  Internet  → STUN P2P  → ISP speed
```

---

## Deploy on Railway (5 minutes)

### Step 1: Upload to GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USER/p2pshare.git
git push -u origin main
```

### Step 2: Deploy
1. Go to [railway.app](https://railway.app)
2. New Project → Deploy from GitHub
3. Select your repo
4. Railway auto-detects Python + deploys ✅

### Step 3: Done!
Railway gives you a URL like:
```
https://p2pshare-production.up.railway.app
```

Share this URL with anyone worldwide!

---

## Run Locally (WiFi only)

```bash
pip install -r requirements.txt
python server.py
# Open: http://localhost:5683
```

---

## Usage

### Sender:
1. Choose **Local WiFi** or **Over Internet** mode
2. Select files (drag & drop supported)
3. Click **Start Sharing** → get 4-digit code + QR
4. Share code/QR with receiver

### Receiver:
1. Scan QR **or** open URL **or** enter code manually
2. Files transfer directly — no server storage!
3. Multiple files auto-download as ZIP

---

## Features

| Feature | Details |
|---------|---------|
| Max size (WiFi) | 5 GB |
| Max size (Internet) | 500 MB |
| Multiple files | ✅ |
| Multiple receivers | ✅ (each gets their own P2P connection) |
| ZIP download | ✅ (auto, browser-side) |
| QR code | ✅ (separate WiFi + Internet QRs) |
| Server storage | ❌ Never — files go directly browser-to-browser |
| Speed (LAN) | Up to ~500 MB/s |

---

## Environment Variables (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 5683 | Server port (Railway sets this automatically) |
| `SECRET_KEY` | auto | Flask session secret |

---

## Tech Stack

- **Backend**: Python · Flask · Flask-SocketIO · eventlet
- **Transfer**: WebRTC DataChannel (browser ↔ browser)
- **QR**: qrcode (Pillow)
- **ZIP**: JSZip (browser-side, no server)
- **Deployment**: Railway · Gunicorn · Nixpacks

---

© 2025 THEDARKGEEKDC
