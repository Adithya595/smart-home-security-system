from machine import Pin, ADC, time_pulse_us, WDT
import network
import urequests
import time
import math

# ================== WIFI & FIREBASE CONFIG ==================
WIFI_SSID     = "adithya"
WIFI_PASSWORD = "raspberrypipico"

FIREBASE_URL    = "https://smart-security-system-3e054-default-rtdb.asia-southeast1.firebasedatabase.app"
FIREBASE_SECRET = "2UFNvdlJttJl8cbmKi55muJIMGePOQTzZHuKq7AI"

SEND_INTERVAL_MS = 2000
READ_INTERVAL_MS = 2000

ENABLE_WATCHDOG = False   # keep False while developing/testing in Thonny.
                          # a running WDT will reset the board if not fed
                          # every loop - very confusing mid-debug. Flip to
                          # True only once everything else works reliably.

# ================== SECURITY CONFIG ==================
PASSCODE = "SSL"                # typed into the Node-RED dashboard to disarm
MAX_FAILED_ATTEMPTS = 3
LOCKOUT_DURATION_MS = 30000

CALIBRATION_DURATION_MS = 8000
CALIBRATION_SAMPLE_INTERVAL_MS = 200

TEMP_STD_MULTIPLIER = 4.0     # flag heat anomaly if temp > mean + 4*std
MIN_TEMP_STD = 0.5            # floor so a too-stable calibration doesn't
                               # make the system oversensitive

DIST_STD_MULTIPLIER = 4.0     # flag intrusion if distance < mean - 4*std
MIN_DIST_STD = 3.0

STATE_FILE = "/armed_state.txt"

# ================== STATE MACHINE CONSTANTS ==================
STATE_CALIBRATING = 0
STATE_DISARMED    = 1
STATE_ARMED_HOME  = 2
STATE_ARMED_AWAY  = 3
STATE_ALARM       = 4
STATE_LOCKOUT     = 5

STATE_NAMES = {
    STATE_CALIBRATING: "CALIBRATING",
    STATE_DISARMED:    "DISARMED",
    STATE_ARMED_HOME:  "ARMED-HOME",
    STATE_ARMED_AWAY:  "ARMED-AWAY",
    STATE_ALARM:       "ALARM",
    STATE_LOCKOUT:     "LOCKOUT",
}

# ---------------- PIN SETUP ----------------
led1   = Pin(2, Pin.OUT)     # system/armed indicator
led2   = Pin(3, Pin.OUT)     # alarm/warning indicator
# NOTE: physical buttons (pins 4, 5) are no longer read by this version -
# all arming/disarming now happens through the Node-RED dashboard via
# Firebase. The buttons can stay wired, they're just unused in software.
buzzer = Pin(6, Pin.OUT)
relay  = Pin(7, Pin.OUT)     # door lock actuator: ON = locked, OFF = unlocked

trigger = Pin(15, Pin.OUT)
echo = Pin(14, Pin.IN)

lm35 = ADC(27)

from gpio_lcd import GpioLcd
lcd = GpioLcd(rs_pin=Pin(8), enable_pin=Pin(9),
              d4_pin=Pin(10), d5_pin=Pin(11),
              d6_pin=Pin(12), d7_pin=Pin(13))

# ---------------- GLOBAL STATE ----------------
current_state = STATE_CALIBRATING
previous_armed_state = STATE_DISARMED   # what LOCKOUT should return to
alarm_cause = None                       # "intrusion" or "fire"

temp_baseline_mean = 0.0
temp_baseline_std = 0.0
dist_baseline_mean = 0.0
dist_baseline_std = 0.0

failed_attempts = 0
lockout_start_time = 0

last_send_time = 0
last_read_time = 0
last_sensor_read = 0
last_lcd_update = 0
buzzer_toggle_time = 0
buzzer_is_on = False

cached_temp = 0.0
cached_distance = 0.0
wifi_ok = False


# ------------------------------------------------
# WiFi
# ------------------------------------------------
def connect_wifi():
    lcd.clear()
    lcd.putstr("Connecting")
    lcd.move_to(0, 1)
    lcd.putstr("to WiFi...")

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.config(pm=0xa11140)
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    timeout = 20
    while not wlan.isconnected() and timeout > 0:
        time.sleep(1)
        timeout -= 1

    if wlan.isconnected():
        lcd.clear()
        lcd.putstr("WiFi Connected")
        lcd.move_to(0, 1)
        lcd.putstr(wlan.ifconfig()[0])
        time.sleep(1)
        return True
    else:
        lcd.clear()
        lcd.putstr("WiFi FAILED")
        lcd.move_to(0, 1)
        lcd.putstr("Offline mode")
        time.sleep(1)
        return False


# ------------------------------------------------
# Sensors
# ------------------------------------------------
def get_distance():
    trigger.low()
    time.sleep_us(2)
    trigger.high()
    time.sleep_us(10)
    trigger.low()
    try:
        pulse_time = time_pulse_us(echo, 1, 30000)
    except OSError:
        return None
    if pulse_time < 0:
        return None
    return (pulse_time * 0.0343) / 2


def get_temperature():
    adc = lm35.read_u16()
    voltage = adc * (3.3 / 65535)
    return voltage * 100


# ------------------------------------------------
# Calibration - learns this room's "normal" baseline
# ------------------------------------------------
def calibrate():
    lcd.clear()
    lcd.putstr("Calibrating")
    lcd.move_to(0, 1)
    lcd.putstr("Keep area clear")

    temp_samples = []
    dist_samples = []

    elapsed = 0
    while elapsed < CALIBRATION_DURATION_MS:
        t = get_temperature()
        d = get_distance()
        temp_samples.append(t)
        if d is not None:
            dist_samples.append(d)
        time.sleep_ms(CALIBRATION_SAMPLE_INTERVAL_MS)
        elapsed += CALIBRATION_SAMPLE_INTERVAL_MS

    t_mean = sum(temp_samples) / len(temp_samples)
    t_var = sum((x - t_mean) ** 2 for x in temp_samples) / len(temp_samples)
    t_std = math.sqrt(t_var)

    if len(dist_samples) > 0:
        d_mean = sum(dist_samples) / len(dist_samples)
        d_var = sum((x - d_mean) ** 2 for x in dist_samples) / len(dist_samples)
        d_std = math.sqrt(d_var)
    else:
        d_mean, d_std = 100.0, 10.0   # fallback if ultrasonic gave no readings

    lcd.clear()
    lcd.putstr("Calibration OK")
    time.sleep(1)

    return t_mean, t_std, d_mean, d_std


def check_temp_anomaly(temp):
    eff_std = max(temp_baseline_std, MIN_TEMP_STD)
    return temp > (temp_baseline_mean + TEMP_STD_MULTIPLIER * eff_std)


def check_distance_anomaly(dist):
    if dist is None:
        return False
    eff_std = max(dist_baseline_std, MIN_DIST_STD)
    return dist < (dist_baseline_mean - DIST_STD_MULTIPLIER * eff_std)


# ------------------------------------------------
# State persistence (survives power loss)
# ------------------------------------------------
def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            f.write(str(state))
    except Exception as e:
        print("State save failed:", e)


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            s = int(f.read())
            if s in (STATE_DISARMED, STATE_ARMED_HOME, STATE_ARMED_AWAY):
                return s
    except Exception:
        pass
    return STATE_DISARMED


# ------------------------------------------------
# State transition helper - keeps all side-effects in one place
# ------------------------------------------------
def enter_state(new_state, cause=None):
    global current_state, alarm_cause, failed_attempts

    current_state = new_state
    alarm_cause = cause
    print("State ->", STATE_NAMES[new_state], "cause:", cause)

    if new_state == STATE_DISARMED:
        relay.value(0)          # unlock door
        led1.value(0)
        led2.value(0)
        buzzer.value(0)
        failed_attempts = 0
        save_state(new_state)

    elif new_state in (STATE_ARMED_HOME, STATE_ARMED_AWAY):
        relay.value(1)          # lock door
        led1.value(1)
        led2.value(0)
        buzzer.value(0)
        save_state(new_state)

    elif new_state == STATE_ALARM:
        led2.value(1)
        if cause == "fire":
            relay.value(0)      # unlock for safety egress
        else:
            relay.value(1)      # keep locked against intrusion

    elif new_state == STATE_LOCKOUT:
        led1.value(0)
        led2.value(1)


# (Physical button handling removed - see check_remote_commands() below,
#  which now handles arming AND disarming, all driven from Node-RED.)


# ------------------------------------------------
# LOCKOUT resolution
# ------------------------------------------------
def handle_lockout():
    now = time.ticks_ms()
    if time.ticks_diff(now, lockout_start_time) >= LOCKOUT_DURATION_MS:
        enter_state(previous_armed_state)


# ------------------------------------------------
# Buzzer patterns (non-blocking)
# ------------------------------------------------
def update_buzzer():
    global buzzer_toggle_time, buzzer_is_on
    now = time.ticks_ms()

    if current_state == STATE_ALARM and alarm_cause == "intrusion":
        buzzer.value(1)   # continuous tone

    elif current_state == STATE_ALARM and alarm_cause == "fire":
        if time.ticks_diff(now, buzzer_toggle_time) > 150:
            buzzer_is_on = not buzzer_is_on
            buzzer.value(buzzer_is_on)
            buzzer_toggle_time = now

    elif current_state == STATE_LOCKOUT:
        if time.ticks_diff(now, buzzer_toggle_time) > 1000:
            buzzer_is_on = not buzzer_is_on
            buzzer.value(buzzer_is_on)
            buzzer_toggle_time = now

    else:
        buzzer.value(0)


# ------------------------------------------------
# Firebase - push status, pull ARM-ONLY remote command
# ------------------------------------------------
def send_to_firebase(temp, distance):
    url = "{}/.json?auth={}".format(FIREBASE_URL, FIREBASE_SECRET)
    payload = {
        "State": STATE_NAMES[current_state],
        "AlarmCause": alarm_cause if alarm_cause else "none",
        "FailedAttempts": failed_attempts,
        "Sensors": {
            "Temperature": temp,
            "Distance": distance if distance is not None else -1,
        },
        "Timestamp": {"time": time.time()},
    }
    try:
        response = urequests.patch(url, json=payload)
        response.close()
    except Exception as e:
        print("Firebase send failed:", e)


def check_remote_commands():
    """All arming AND disarming now happens through Node-RED / Firebase,
    since the physical buttons proved unreliable. The passcode is still
    required to disarm - it's just typed into a dashboard field instead
    of tapped on a button. Each command self-clears after being read so
    it doesn't get re-applied on the next poll."""
    global current_state, previous_armed_state, lockout_start_time, failed_attempts

    url = "{}/Command.json?auth={}".format(FIREBASE_URL, FIREBASE_SECRET)
    try:
        response = urequests.get(url)
        data = response.json()
        response.close()
    except Exception as e:
        print("Firebase command read failed:", e)
        return

    if not data:
        return

    mode = data.get("Mode", "NONE")
    passcode_attempt = data.get("Passcode", "")

    command_consumed = False

    # ---- Arming (only from DISARMED) ----
    if mode in ("ARM_HOME", "ARM_AWAY") and current_state == STATE_DISARMED:
        enter_state(STATE_ARMED_HOME if mode == "ARM_HOME" else STATE_ARMED_AWAY)
        command_consumed = True

    # ---- Disarming (from any armed/alarm state) ----
    if passcode_attempt:
        if current_state in (STATE_ARMED_HOME, STATE_ARMED_AWAY, STATE_ALARM):
            if passcode_attempt == PASSCODE:
                enter_state(STATE_DISARMED)
            else:
                failed_attempts += 1
                if failed_attempts >= MAX_FAILED_ATTEMPTS:
                    previous_armed_state = current_state if current_state != STATE_ALARM else STATE_ARMED_AWAY
                    lockout_start_time = time.ticks_ms()
                    enter_state(STATE_LOCKOUT)
        command_consumed = True

    # ---- Clear consumed commands so they don't re-trigger next poll ----
    if command_consumed:
        try:
            clear_url = "{}/Command.json?auth={}".format(FIREBASE_URL, FIREBASE_SECRET)
            urequests.patch(clear_url, json={"Mode": "NONE", "Passcode": ""}).close()
        except Exception as e:
            print("Firebase command clear failed:", e)


# ------------------------------------------------
# LCD
# ------------------------------------------------
def update_lcd():
    global last_lcd_update
    now = time.ticks_ms()
    if time.ticks_diff(now, last_lcd_update) < 500:
        return
    last_lcd_update = now

    lcd.clear()
    lcd.move_to(0, 0)

    if current_state == STATE_ALARM:
        lcd.putstr("ALARM:{}".format(alarm_cause.upper() if alarm_cause else ""))
        lcd.move_to(0, 1)
        lcd.putstr("Enter code!")
    elif current_state == STATE_LOCKOUT:
        remaining = max(0, LOCKOUT_DURATION_MS - time.ticks_diff(time.ticks_ms(), lockout_start_time))
        lcd.putstr("LOCKED OUT")
        lcd.move_to(0, 1)
        lcd.putstr("Wait {}s".format(remaining // 1000))
    else:
        lcd.putstr("T:{:.1f}C D:{:.0f}".format(cached_temp, cached_distance if cached_distance else 0))
        lcd.move_to(0, 1)
        lcd.putstr(STATE_NAMES[current_state])


# ================================================================
# BOOT SEQUENCE
# ================================================================
lcd.clear()
lcd.putstr(" SECURITY SYS")
lcd.move_to(0, 1)
lcd.putstr("Booting...")
time.sleep(1)

wifi_ok = connect_wifi()

temp_baseline_mean, temp_baseline_std, dist_baseline_mean, dist_baseline_std = calibrate()

restored_state = load_state()
enter_state(restored_state)

if ENABLE_WATCHDOG:
    wdt = WDT(timeout=8000)

# ================================================================
# MAIN LOOP - runs fast (~20ms) for responsive button timing.
# Slower duties (sensors, LCD, Firebase) are time-gated internally.
# ================================================================
while True:

    if ENABLE_WATCHDOG:
        wdt.feed()

    now = time.ticks_ms()

    if current_state == STATE_LOCKOUT:
        handle_lockout()

    # ---- sensor read (every 500ms) ----
    if time.ticks_diff(now, last_sensor_read) > 500:
        cached_temp = get_temperature()
        cached_distance = get_distance()
        last_sensor_read = now

        if current_state in (STATE_ARMED_HOME, STATE_ARMED_AWAY):
            if check_temp_anomaly(cached_temp):
                enter_state(STATE_ALARM, cause="fire")
            elif current_state == STATE_ARMED_AWAY and check_distance_anomaly(cached_distance):
                enter_state(STATE_ALARM, cause="intrusion")

    update_buzzer()
    update_lcd()

    # ---- Firebase (every 2s) ----
    if wifi_ok:
        if time.ticks_diff(now, last_send_time) > SEND_INTERVAL_MS:
            send_to_firebase(cached_temp, cached_distance)
            last_send_time = now
        if time.ticks_diff(now, last_read_time) > READ_INTERVAL_MS:
            check_remote_commands()
            last_read_time = now

    time.sleep_ms(100)
