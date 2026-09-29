import threading
import time

from consult2elm.data import DataStore, EngineData
from consult2elm.status_led import StatusLed, blink_count


def test_blink_patterns():
    assert blink_count(False, False) == 1
    assert blink_count(True, False) == 2
    assert blink_count(False, True) == 3
    assert blink_count(True, True) is None


class FakeServer:
    client_count = 1


def test_led_steady_when_all_ok_and_trigger_restored(tmp_path):
    (tmp_path / "trigger").write_text("none [mmc0] timer heartbeat")
    (tmp_path / "brightness").write_text("0")
    store = DataStore()
    store.update(EngineData(rpm=800, coolant_c=80, speed_kmh=0, battery_v=14.0), valid_for=60)
    stop = threading.Event()
    led = StatusLed(store, FakeServer(), stop, led_path=str(tmp_path))
    led.start()
    time.sleep(0.3)
    assert (tmp_path / "brightness").read_text() == "1"
    stop.set()
    led.join(timeout=5)
    assert (tmp_path / "trigger").read_text() == "mmc0"
