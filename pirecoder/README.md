# Raspberry Pi Audio Recorder

Control audio recording on your Raspberry Pi from any phone or browser on the same Wi-Fi network.

---

## What it does

- Start / Stop recording with one tap from your phone
- Add an optional label to each recording (e.g. "keynote", "panel-1")
- Live timer shows how long you've been recording
- List all saved recordings — download or delete them directly from the browser
- Multiple devices can watch the status in real time (WebSocket)

---

## Raspberry Pi Setup

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv portaudio19-dev
```

### 2. Clone / copy the project

```bash
cd ~
# copy the project folder here, or git clone your repo
```

### 3. Create a virtual environment and install packages

```bash
cd ~/audio_record
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Check your microphone

```bash
arecord -l          # lists capture hardware
```

If your mic shows up (e.g. card 1, device 0), set the default:

```bash
# In ~/.asoundrc  (create if it doesn't exist)
pcm.!default {
    type hw
    card 1          # change to your card number
}
ctl.!default {
    type hw
    card 1
}
```

### 5. Run the server

```bash
source venv/bin/activate
python app.py
```

The server starts on **port 5000**.

---

## Connect from your phone

1. Make sure your phone is on the **same Wi-Fi** as the Raspberry Pi.
2. Find the Pi's IP address:
   ```bash
   hostname -I
   ```
3. Open your phone browser and go to:
   ```
   http://<pi-ip-address>:5000
   ```
   e.g. `http://192.168.1.42:5000`

---

## Auto-start on boot (optional)

Create a systemd service so the recorder starts automatically:

```bash
sudo nano /etc/systemd/system/audio-recorder.service
```

Paste:

```ini
[Unit]
Description=Audio Recorder Web App
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/audio_record
ExecStart=/home/pi/audio_record/venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable audio-recorder
sudo systemctl start audio-recorder
```

---

## Recordings

All `.wav` files are saved in the `recordings/` folder on the Pi.  
You can download them directly from the web UI or via SCP:

```bash
scp pi@<pi-ip>:~/audio_record/recordings/*.wav ./
```

---

## Audio quality

Default settings (editable in `app.py`):

| Setting   | Value  |
|-----------|--------|
| Format    | 16-bit PCM |
| Channels  | Mono (1) |
| Sample rate | 44100 Hz |
| Container | WAV |

Change `CHANNELS = 2` for stereo if your mic supports it.
