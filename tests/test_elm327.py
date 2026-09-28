from consult2elm.data import DataStore, EngineData
from consult2elm.elm327 import Elm327, supported_pids_mask


def make(engine=EngineData(rpm=1725.0, coolant_c=88, speed_kmh=64, battery_v=14.08)):
    store = DataStore()
    if engine is not None:
        store.update(engine, valid_for=60)
    return Elm327(store)


def send(elm, text):
    return elm.feed(text.encode() + b"\r").decode()


def test_reset_and_echo():
    elm = make()
    assert send(elm, "ATZ") == "ATZ\r\rELM327 v1.5\r\r>"
    assert send(elm, "ATE0") == "ATE0\rOK\r\r>"
    assert send(elm, "ATI") == "ELM327 v1.5\r\r>"


def test_linefeeds():
    elm = make()
    send(elm, "ATE0")
    assert send(elm, "ATL1") == "OK\r\n\r\n>"


def test_rpm_coolant_speed():
    elm = make()
    send(elm, "ATE0")
    assert send(elm, "010C") == "41 0C 1A F4\r\r>"   # 1725 rpm * 4 = 0x1AF4
    assert send(elm, "0105") == "41 05 80\r\r>"      # 88 + 40 = 0x80
    assert send(elm, "010D") == "41 0D 40\r\r>"      # 64 km/h


def test_spaces_off_and_response_count_digit():
    elm = make()
    send(elm, "ATE0")
    send(elm, "ATS0")
    assert send(elm, "010C1") == "410C1AF4\r\r>"


def test_headers_iso9141_with_checksum():
    elm = make()
    send(elm, "ATE0")
    send(elm, "ATH1")
    out = send(elm, "010D")
    frame = bytes.fromhex(out.split("\r")[0])
    assert frame[:5] == bytes((0x48, 0x6B, 0x10, 0x41, 0x0D))
    assert frame[-1] == sum(frame[:-1]) & 0xFF


def test_supported_pids():
    mask = supported_pids_mask(0)
    for pid in (0x01, 0x05, 0x0C, 0x0D):
        assert mask & (1 << (32 - pid))
    elm = make(engine=None)
    send(elm, "ATE0")
    assert send(elm, "0100") == "41 00 88 18 00 00\r\r>"
    assert send(elm, "0120") == "NO DATA\r\r>"


def test_no_data_when_engine_values_stale():
    elm = make(engine=None)
    send(elm, "ATE0")
    assert send(elm, "010C") == "NO DATA\r\r>"
    assert send(elm, "ATIGN") == "OFF\r\r>"


def test_repeat_last_command_on_empty_line():
    elm = make()
    send(elm, "ATE0")
    send(elm, "010D")
    assert elm.feed(b"\r").decode() == "41 0D 40\r\r>"


def test_protocol_and_voltage():
    elm = make()
    send(elm, "ATE0")
    assert send(elm, "ATSP0") == "OK\r\r>"
    assert send(elm, "ATDP") == "AUTO, ISO 9141-2\r\r>"
    assert send(elm, "ATDPN") == "A3\r\r>"
    assert send(elm, "AT RV") == "14.1V\r\r>"


def test_dtcs_and_unknown():
    elm = make()
    send(elm, "ATE0")
    assert send(elm, "03") == "43 00 00 00 00 00 00\r\r>"
    assert send(elm, "0902") == "NO DATA\r\r>"
    assert send(elm, "HELLO") == "?\r\r>"
    assert send(elm, "ATCAF0") == "OK\r\r>"


def test_command_split_across_packets():
    elm = make()
    send(elm, "ATE0")
    assert elm.feed(b"01") == b""
    assert elm.feed(b"0D\r") == b"41 0D 40\r\r>"
