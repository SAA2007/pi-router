# nek0-router ESP32-C3 status indicator & button controller (MicroPython)
# Hardware: ESP32-C3 SuperMini / Mini
#   - GPIO 8: Onboard blue status LED (active-LOW: 0 = ON, 1 = OFF)
#   - GPIO 9: Onboard BOOT button (active-LOW, internal pull-up)
# Serial protocol over USB CDC (/dev/ttyACM0, 115200 baud):
#   Pi -> ESP32:
#     STATE:OK         Solid steady ON (Ethernet internet OK)
#     STATE:HOTSPOT    Slow double-blink every 3.5s (Phone hotspot failover)
#     STATE:NO_NET     Slow 1s blink (0.8s ON / 0.8s OFF - No internet)
#     STATE:AP_DOWN    Slow triple-blink every 4s (Wi-Fi AP wlan0 is down)
#     STATE:OFF        LED completely off (night/stealth mode)
#     BLINK:<n>        Slowly blink <n> times (connected Wi-Fi device count)
#   ESP32 -> Pi:
#     BTN:SHORT        Short press on BOOT button (< 2s) -> toggle LED power / show status
#     BTN:LONG         Long press on BOOT button (>= 3s) -> toggle hotspot / action

import select
import sys
import time
from machine import Pin, WDT

# Enable 2.4 GHz RF beacon on Channel 6 (2437 MHz) for room RF presence/motion sensing.
# Runs entirely in ESP32-C3 Wi-Fi MAC hardware (0% CPU blocking on LED or BOOT button).
try:
    import network
    _ap = network.WLAN(network.AP_IF)
    _ap.active(True)
    _ap.config(essid="nek0-sense", channel=6, hidden=True, authmode=0)
except Exception:
    _ap = None

LED_PIN = 8
BTN_PIN = 9

led = Pin(LED_PIN, Pin.OUT, value=1)  # active-LOW: 1 = OFF, 0 = ON
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


def set_led(on):
    led.value(0 if on else 1)


def blink_count(n):
    set_led(False)
    time.sleep_ms(400)
    for _ in range(max(1, min(20, n))):
        feed()
        set_led(True)
        time.sleep_ms(400)
        set_led(False)
        time.sleep_ms(400)
    time.sleep_ms(500)


poll = select.poll()
poll.register(sys.stdin, select.POLLIN)

state = "OK"
local_off = False
last_rx = time.ticks_ms()
btn_down_at = 0
btn_long_sent = False

while True:
    feed()
    now = time.ticks_ms()

    # 1. Read complete lines from Pi over USB serial
    while poll.poll(0):
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith("STATE:"):
            new_st = line.split(":", 1)[1].strip()
            if new_st != state:
                state = new_st
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

    # 2. Check BOOT button (GPIO 9, active-LOW)
    #    Short press: toggle LED power ON/OFF right from the button!
    #    Long press (3s): send BTN:LONG to Pi
    if btn.value() == 0:
        if btn_down_at == 0:
            btn_down_at = now
            btn_long_sent = False
        elif not btn_long_sent and time.ticks_diff(now, btn_down_at) >= 3000:
            btn_long_sent = True
            sys.stdout.write("BTN:LONG\n")
            blink_count(2)
    else:
        if btn_down_at != 0:
            held = time.ticks_diff(now, btn_down_at)
            btn_down_at = 0
            if 40 <= held < 3000 and not btn_long_sent:
                local_off = not local_off
                sys.stdout.write("BTN:SHORT\n")

    # 3. If locally toggled OFF via button or Pi sent STATE:OFF, keep LED off
    #    (unless there is an active NO_NET / AP_DOWN error!)
    if (local_off or state == "OFF") and state not in ("NO_NET", "AP_DOWN"):
        set_led(False)
        time.sleep_ms(40)
        continue

    # 4. If Pi hasn't sent a heartbeat in >45s, turn LED off
    if time.ticks_diff(now, last_rx) > 45000:
        set_led(False)
        time.sleep_ms(40)
        continue

    # 5. Calm, slow LED patterns
    if state == "OK":
        # Solid steady ON (zero blinking)
        set_led(True)
    elif state == "HOTSPOT":
        # Slow double-blink every 3.5s (400ms ON, 400ms OFF, 400ms ON, 2300ms OFF)
        phase = now % 3500
        set_led(phase < 400 or (800 <= phase < 1200))
    elif state == "NO_NET":
        # Calm 1.6s slow blink (800ms ON, 800ms OFF)
        phase = now % 1600
        set_led(phase < 800)
    elif state == "AP_DOWN":
        # Slow triple-blink every 4s
        phase = now % 4000
        set_led(phase < 350 or (700 <= phase < 1050) or (1400 <= phase < 1750))
    else:
        set_led(False)

    time.sleep_ms(40)
