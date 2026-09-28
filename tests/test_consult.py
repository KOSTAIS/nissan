import time

from consult2elm.consult import (
    FrameParser,
    ConsultClient,
    build_stream_request,
    decode_frame,
)
from consult2elm.fake_ecu import FakeConsultECU


def test_stream_request():
    assert build_stream_request() == bytes.fromhex("5A005A015A085A0B5A0CF0")


def test_decode_frame():
    data = decode_frame(bytes((0x00, 0x8A, 0x82, 0x1E, 0xB0)))
    assert data.rpm == 138 * 12.5
    assert data.coolant_c == 80
    assert data.speed_kmh == 60
    assert data.battery_v == 14.08


def test_parser_skips_echo_and_handles_ff_in_data():
    parser = FrameParser(5)
    echo = bytes.fromhex("A500A501A508A50BA50C")
    f1 = bytes((0xFF, 0x05, 0xFF, 0xFF, 0x82, 0x1E, 0xB0))
    f2 = bytes((0xFF, 0x05, 0x00, 0x40, 0x82, 0x1E, 0xB0))
    frames = parser.feed(echo + f1 + f2[:3])
    assert frames == [f1[2:]]
    assert parser.feed(f2[3:] + b"\xFF") == [f2[2:]]


def test_parser_resyncs_after_garbage():
    parser = FrameParser(5)
    good = bytes((0xFF, 0x05, 1, 2, 3, 4, 5))
    assert parser.feed(b"\x05\xFF\x05\x09" + good + good) == [good[2:]]


def test_client_against_fake_ecu():
    ecu = FakeConsultECU()
    ecu.rpm, ecu.coolant_c, ecu.speed_kmh = 2500, 90, 50
    client = ConsultClient("fake", serial_factory=lambda p, b: ecu)
    client.open()
    assert client.initialise()
    client.start_stream()
    frames = []
    deadline = time.monotonic() + 2
    while not frames and time.monotonic() < deadline:
        frames = client.read_frames()
    assert frames[-1].rpm == 2500
    assert frames[-1].coolant_c == 90
    assert frames[-1].speed_kmh == 50
    client.stop_stream()


def test_init_fails_with_ignition_off():
    ecu = FakeConsultECU(ignition=False)
    client = ConsultClient("fake", serial_factory=lambda p, b: ecu)
    client.open()
    assert not client.initialise(timeout=0.3)
