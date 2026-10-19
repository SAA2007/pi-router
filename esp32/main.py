# nek0-router ESP32-C3 status indicator & button controller (MicroPython)
# Hardware: ESP32-C3 SuperMini / Mini
#   - GPIO 8: Onboard blue status LED (active-LOW: 0 = ON, 1 = OFF)
#   - GPIO 9: Onboard BOOT button (active-LOW, internal pull-up)
# Serial protocol over USB CDC (/dev/ttyACM0, 115200 baud):
#   Pi -> ESP32:
#     STATE:OK         Steady glow + brief heartbeat wink every 5s (Ethernet internet OK)
#     STATE:HOTSPOT    Double-pulse every 2s (Running on phone hotspot failover)
#     STATE:NO_NET     Fast 5 Hz urgent strobe (No internet on Ethernet or Hotspot)
#     STATE:AP_DOWN    Triple-blink every 2s (Wi-Fi AP wlan0 / hostapd is down)
#     STATE:OFF        LED completely off (night mode when healthy)
#     BLINK:<n>        Blink <n> times distinctly (e.g. number of online Wi-Fi users)
#   ESP32 -> Pi:
#     BTN:SHORT        Short press on BOOT button (< 2s) -> blink online user count + poll
#     BTN:LONG         Long press on BOOT button (>= 2.5s) -> toggle hotspot / auto uplink

import select
import sys
import time
from machine import Pin, PWM, WDT

LED_PIN = 8
BTN_PIN = 9

# Active-LOW PWM on GPIO 8: duty_u16 = 65535 is OFF, 0 is 100% ON
pwm = PWM(Pin(LED_PIN, Pin.OUT), freq=1000, duty_u16=65535)
btn = Pin(BTN_PIN, Pin.IN, Pin.PULL_UP)

try:
    wdt = WDT(timeout=10000)
except Exception:
    wdt = None


def feed():
    if wdt:
        try:
            wdt.feed()
        except Exception:
            pass


def set_led(brightness):
    """brightness: 0.0 (OFF) to 1.0 (full ON) for active-LOW GPIO 8 LED."""
    if brightness <= 0.0:
        pwm.duty_u16(65535)
    elif brightness >= 1.0:
        pwm.duty_u16(0)
    else:
        pwm.duty_u16(int(65535 * (1.0 - brightness)))


def blink_count(n):
    set_led(0.0)
    time.sleep_ms(250)
    for _ in range(max(1, min(20, n))):
        feed()
        set_led(1.0)
        time.sleep_ms(180)
        set_led(0.0)
        time.sleep_ms(220)
    time.sleep_ms(300)


# Startup self-test: 3 quick crisp flashes so user sees ESP32 booted
for _ in range(3):
    set_led(1.0)
    time.sleep_ms(80)
    set_led(0.0)
    time.sleep_ms(80)

poll = select.poll()
poll.register(sys.stdin, select.POLLIN)

state = "NO_NET"
last_rx = time.ticks_ms()
buf = ""
btn_down_at = 0
btn_long_sent = False

while True:
    feed()
    now = time.ticks_ms()

    # 1. Read non-blocking commands from Pi over USB serial
    while poll.poll(0):
        ch = sys.stdin.read(1)
        if not ch:
            break
        if ch == "\n":
            line = buf.strip()
            buf = ""
            if line.startswith("STATE:"):
                state = line.split(":", 1)[1].strip()
                last_rx = now
            elif line.startswith("BLINK:"):
                try:
                    blink_count(int(line.split(":", 1)[1]))
                except Exception:
                    pass
                last_rx = now
            elif line == "PING":
                last_rx = now
                sys.stdout.write("PONG\n")
        elif len(buf) < 64:
            buf += ch

    # 2. Check BOOT button (GPIO 9, active-LOW)
    if btn.value() == 0:
        if btn_down_at == 0:
            btn_down_at = now
            btn_long_sent = False
        elif not btn_long_sent and time.ticks_diff(now, btn_down_at) >= 2500:
            btn_long_sent = True
            sys.stdout.write("BTN:LONG\n")
            blink_count(2)
    else:
        if btn_down_at != 0:
            held = time.ticks_diff(now, btn_down_at)
            btn_down_at = 0
            if 40 <= held < 2500 and not btn_long_sent:
                sys.stdout.write("BTN:SHORT\n")

    # 3. If Pi hasn't sent a heartbeat in >30s, show disconnected blip
    if time.ticks_diff(now, last_rx) > 30000:
        phase = now % 3000
        set_led(1.0 if phase < 40 else 0.0)
        time.sleep_ms(20)
        continue

    # 4. Render LED pattern for current router state
    if state == "OK":
        # Solid ON (70% comfortable brightness) with a 60ms heartbeat wink every 4s
        phase = now % 4000
        set_led(0.0 if phase < 60 else 0.7)
    elif state == "HOTSPOT":
        # Double-pulse every 2s: ON 120ms, OFF 120ms, ON 120ms, OFF 1640ms
        phase = now % 2000
        set_led(1.0 if (phase < 120 or 240 <= phase < 360) else 0.0)
    elif state == "NO_NET":
        # Urgent fast 5 Hz strobe: ON 100ms, OFF 100ms
        phase = now % 200
        set_led(1.0 if phase < 100 else 0.0)
    elif state == "AP_DOWN":
        # Triple-blink every 2s (Wi-Fi AP error)
        phase = now % 2000
        on = (phase < 100) or (200 <= phase < 300) or (400 <= phase < 500)
        set_led(1.0 if on else 0.0)
    elif state == "OFF":
        set_led(0.0)
    else:
        set_led(0.2)

    time.sleep_ms(20)
