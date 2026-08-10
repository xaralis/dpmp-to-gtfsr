from dpmp_gtfs.protocol import PROTOCOL_WINDOW_MS, app_protocol

SEED = "your-public-protocol-seed"


def test_signature_matches_the_web_app():
    # counter = 1000, i.e. unix_ms 900_000_000 .. 900_899_999
    assert app_protocol(SEED, now_ms=900_000_000) == app_protocol(SEED, now_ms=900_899_999)


def test_signature_rotates_every_fifteen_minutes():
    before = app_protocol(SEED, now_ms=900_000_000)
    after = app_protocol(SEED, now_ms=900_000_000 + PROTOCOL_WINDOW_MS)
    assert before != after


def test_signature_is_hex_sha256():
    sig = app_protocol(SEED, now_ms=900_000_000)
    assert len(sig) == 64
    assert set(sig) <= set("0123456789abcdef")
