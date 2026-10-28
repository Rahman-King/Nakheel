#!/usr/bin/env python3
"""
NAKHEEL - MODULE 2: DATE GRADER
Raspberry Pi 3B+ · Pi Camera · 0.96" SSD1306 OLED (I2C) · 4 push buttons · PAM8403 + 8 ohm speaker
Takes a photo of a date, asks Google Gemini for TYPE, GRADE and SIZE, then shows it on the OLED
and speaks it out loud.

WIRING (BCM numbers, physical pin in brackets)
  OLED   VCC -> 3V3 (1)   GND -> GND (9)   SDA -> GPIO2 (3)   SCL -> GPIO3 (5)
  Button PHOTO  -> GPIO17 (11) and GND (20)
  Button VOL +  -> GPIO27 (13) and GND (20)
  Button VOL -  -> GPIO22 (15) and GND (20)
  Button MUTE   -> GPIO23 (16) and GND (20)      (hold MUTE 4 s = safe shutdown)
  Camera ribbon -> CSI camera port
  PAM8403  5V -> 5V (2)   GND -> GND (6)   L-in <- 1k <- GPIO18 (12), 10 nF from L-in to GND-in
           (install.sh adds dtoverlay=audremap,pins=18_19 so the sound comes out of GPIO18)
           L+ / L- -> 8 ohm speaker

Run:  GEMINI_API_KEY=xxxx python3 nakheel_grader.py
"""
import io, json, os, re, shutil, subprocess, threading, time, wave, math, struct
from datetime import datetime
from pathlib import Path

from gpiozero import Button
from picamera2 import Picamera2
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306
from PIL import ImageFont
from google import genai
from google.genai import types

# ------------------------------------------------------------------ settings
API_KEY       = os.environ.get("GEMINI_API_KEY", "PASTE_YOUR_KEY_HERE")
MODEL         = "gemini-3.8-flash"        # stable Flash model
MODEL_BACKUP  = "gemini-flash-latest"     # used automatically if MODEL is unavailable
LANGUAGE      = os.environ.get("NAKHEEL_LANG", "en")   # "en" or "ar" for spoken results
PIN_PHOTO, PIN_UP, PIN_DOWN, PIN_MUTE = 17, 27, 22, 23
VOLUME_STEP   = 10
SAVE_DIR      = Path.home() / "nakheel_results"
SETTINGS_FILE = SAVE_DIR / "settings.json"

PROMPT = """You are an expert date-fruit (Phoenix dactylifera) grader working for farmers in Oman.
The photo shows ONE date on a tray printed with a 10 mm grid. Use the grid squares to measure it.
Return ONLY JSON with these keys:
  "is_date": true/false,
  "type": variety name, e.g. Khalas, Khunaizi, Fard, Naghal, Khasab, Barni, Mabsali, Zabad, Hilali,
          Medjool, Sukkari, Ajwa, Mazafati, or "Unknown",
  "type_confidence": 0-100,
  "grade": "A", "B", "C" or "Reject"   (A = export quality: intact skin, uniform colour, no defects;
          B = minor skin separation or blemishes; C = noticeable defects, still edible/processing;
          Reject = insect damage, mould, fermentation, cracks or dirt),
  "size": "Large", "Medium" or "Small",
  "length_mm": estimated length in mm (number),
  "width_mm": estimated width in mm (number),
  "defects": short list of visible problems (may be empty),
  "advice_en": one short sentence for the farmer in simple English (how to sell / use it),
  "advice_ar": the same advice in simple Arabic
If the photo does not show a date, set is_date to false and leave the other fields empty."""

# ------------------------------------------------------------------ hardware
SAVE_DIR.mkdir(exist_ok=True)
oled = ssd1306(i2c(port=1, address=0x3C), width=128, height=64)
try:
    FONT_BIG = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15)
    FONT     = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
except OSError:
    FONT_BIG = FONT = ImageFont.load_default()

cam = Picamera2()
cam.configure(cam.create_still_configuration(main={"size": (1640, 1232)}))
cam.start()
time.sleep(1.5)                                   # let exposure settle

client = genai.Client(api_key=API_KEY)
busy = threading.Lock()
state = {"last": time.time(), "home": True, "held": False}


def touch():
    state["last"] = time.time(); state["home"] = False


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {"volume": 70, "muted": False}


settings = load_settings()


def save_settings():
    SETTINGS_FILE.write_text(json.dumps(settings))


# ------------------------------------------------------------------ screen
def show(lines, big_first=True):
    """Draw up to 4 lines. First line in bold."""
    with canvas(oled) as d:
        y = 0
        for i, line in enumerate(lines[:4]):
            f = FONT_BIG if (i == 0 and big_first) else FONT
            d.text((0, y), str(line), font=f, fill="white")
            y += 18 if f is FONT_BIG else 14


def show_volume():
    v = 0 if settings["muted"] else settings["volume"]
    with canvas(oled) as d:
        d.text((0, 0), "MUTED" if settings["muted"] else "Volume", font=FONT_BIG, fill="white")
        d.rectangle((0, 30, 127, 46), outline="white")
        d.rectangle((2, 32, 2 + int(123 * v / 100), 44), fill="white")
        d.text((0, 50), f"{v}%", font=FONT, fill="white")


def home_screen():
    show(["NAKHEEL", "Date Grader", "Place a date on", "the tray, press PHOTO"])


# ------------------------------------------------------------------ sound
def amp():
    return 0 if settings["muted"] else settings["volume"]


def beep(freq=880, ms=120):
    if settings["muted"]:
        return
    rate, n = 22050, int(22050 * ms / 1000)
    level = 32767 * amp() / 100 * 0.6
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(level * math.sin(2 * math.pi * freq * i / rate)))
                               for i in range(n)))
    subprocess.run(["aplay", "-q", "-"], input=buf.getvalue())


def say(text_en, text_ar=None):
    if settings["muted"] or not shutil.which("espeak-ng"):
        return
    voice, text = ("ar", text_ar) if (LANGUAGE == "ar" and text_ar) else ("en-gb", text_en)
    a = int(amp() * 1.8)                        # espeak amplitude 0-200
    subprocess.run(["espeak-ng", "-v", voice, "-s", "145", "-a", str(a), text])


# ------------------------------------------------------------------ Gemini
def ask_gemini(jpeg_bytes):
    parts = [types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg"), PROMPT]
    cfg = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2)
    last_err = None
    for model in (MODEL, MODEL_BACKUP):
        try:
            r = client.models.generate_content(model=model, contents=parts, config=cfg)
            txt = re.sub(r"^```(json)?|```$", "", r.text.strip(), flags=re.M).strip()
            return json.loads(txt)
        except Exception as e:                  # wrong model name, network, bad JSON...
            last_err = e
    raise last_err


def spinner(stop_event):
    dots = 0
    while not stop_event.is_set():
        show(["Analysing", "." * (dots % 4 + 1), "Asking Gemini AI", "please wait"])
        dots += 1
        time.sleep(0.4)


# ------------------------------------------------------------------ main action
def take_and_grade():
    if not busy.acquire(blocking=False):        # ignore presses while working
        return
    touch()
    try:
        show(["Hold still", "", "Taking photo..."])
        beep(1200, 80)
        buf = io.BytesIO()
        cam.capture_file(buf, format="jpeg")
        jpeg = buf.getvalue()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        (SAVE_DIR / f"{stamp}.jpg").write_bytes(jpeg)

        stop = threading.Event()
        t = threading.Thread(target=spinner, args=(stop,), daemon=True); t.start()
        try:
            res = ask_gemini(jpeg)
        finally:
            stop.set(); t.join()

        with open(SAVE_DIR / "log.jsonl", "a") as f:
            f.write(json.dumps({"time": stamp, **res}) + "\n")

        if not res.get("is_date"):
            show(["No date seen", "Put ONE date in", "the middle of the", "tray and retry"])
            beep(300, 250)
            say("I could not see a date. Please place one date in the middle of the tray.",
                "لم أتمكن من رؤية تمرة. ضع تمرة واحدة في وسط الصينية.")
            return

        typ, grade, size = res.get("type", "?"), res.get("grade", "?"), res.get("size", "?")
        L, W = res.get("length_mm"), res.get("width_mm")
        dims = f"{L:.0f}x{W:.0f}mm" if isinstance(L, (int, float)) and isinstance(W, (int, float)) else ""
        show([typ, f"Grade: {grade}", f"{size} {dims}", f"Sure: {res.get('type_confidence', '?')}%"])
        beep(660, 90); beep(990, 120)

        defects = res.get("defects") or []
        en = (f"This looks like a {typ} date. Grade {grade}. {size} size. "
              + (f"Problems: {', '.join(defects)}. " if defects else "")
              + res.get("advice_en", ""))
        ar = f"هذه تمرة {typ}. الدرجة {grade}. الحجم {size}. " + res.get("advice_ar", "")
        say(en, ar)
    except Exception as e:
        print("ERROR:", e)
        show(["Error", "Check WiFi or", "API key, then", "press PHOTO again"])
        beep(300, 400)
        say("Something went wrong. Check the internet connection and try again.",
            "حدث خطأ. تحقق من الاتصال بالإنترنت وحاول مرة أخرى.")
    finally:
        touch()
        busy.release()


def volume(delta):
    touch()
    settings["muted"] = False
    settings["volume"] = max(0, min(100, settings["volume"] + delta))
    save_settings(); show_volume(); beep()


def toggle_mute():
    touch()
    settings["muted"] = not settings["muted"]
    save_settings(); show_volume()
    if not settings["muted"]:
        beep()


def shutdown():
    show(["Shutting down", "", "Wait for the green", "LED to stop"])
    say("Shutting down. Goodbye.", "جارٍ الإيقاف. مع السلامة.")
    subprocess.run(["sudo", "shutdown", "-h", "now"])


# ------------------------------------------------------------------ buttons
b_photo = Button(PIN_PHOTO, pull_up=True, bounce_time=0.05)
b_up    = Button(PIN_UP,    pull_up=True, bounce_time=0.05)
b_down  = Button(PIN_DOWN,  pull_up=True, bounce_time=0.05)
b_mute  = Button(PIN_MUTE,  pull_up=True, bounce_time=0.05, hold_time=4)

b_photo.when_pressed = lambda: threading.Thread(target=take_and_grade, daemon=True).start()
b_up.when_pressed    = lambda: volume(+VOLUME_STEP)
b_down.when_pressed  = lambda: volume(-VOLUME_STEP)
def mute_released():
    if not state["held"]:                       # short press = mute, long hold = shutdown
        toggle_mute()


def mute_held():
    state["held"] = True
    shutdown()


b_mute.when_released = mute_released
b_mute.when_held     = mute_held

if __name__ == "__main__":
    home_screen()
    beep(660, 80); beep(880, 120)
    say("Nakheel date grader is ready.", "جهاز نخيل لفرز التمور جاهز.")
    try:
        while True:                             # back to the home screen after 25 s idle
            time.sleep(0.5)
            if not busy.locked() and not state["home"] and time.time() - state["last"] > 25:
                home_screen(); state["home"] = True
    except KeyboardInterrupt:
        oled.clear()

