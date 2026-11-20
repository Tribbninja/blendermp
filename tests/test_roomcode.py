import pytest

from core.roomcode import (
    CODE_LEN,
    FLAG_PIN,
    RoomCodeError,
    decode_room,
    encode_room,
    format_code,
    normalize,
)


def test_roundtrip():
    code = encode_room("192.168.1.42", 51999, FLAG_PIN)
    info = decode_room(code)
    assert info.ip == "192.168.1.42"
    assert info.port == 51999
    assert info.needs_pin is True
    assert len(normalize(code)) == CODE_LEN


def test_normalize_matches_dashed_and_undashed_forms():
    # The directory returns the code without dashes while the host's signed
    # identity descriptor keeps them; identity checks must compare normalized.
    assert normalize("041C1-A24CM-GWPHR") == normalize("041c1a24cmgwphr")
    assert normalize("041C1-A24CM-GWPHR") == "041C1A24CMGWPHR"


def test_code_is_grouped_and_readable():
    code = encode_room("10.0.0.7", 51999)
    assert len(code.split("-")) == 3
    assert all(len(part) == 5 for part in code.split("-"))


def test_decoding_is_forgiving_about_case_and_confusables():
    code = encode_room("127.0.0.1", 52000)
    messy = code.lower().replace("-", " ")
    assert decode_room(messy) == decode_room(code)
    # O/0 and I/1 are visually confusable; normalize maps them.
    assert normalize("O0I1") == "0011"


def test_checksum_rejects_typos():
    code = encode_room("192.168.0.5", 51999)
    text = normalize(code)
    for index in range(len(text)):
        replacement = "Z" if text[index] != "Z" else "Y"
        broken = text[:index] + replacement + text[index + 1 :]
        if broken == text:
            continue
        with pytest.raises(RoomCodeError):
            decode_room(broken)


def test_rejects_bad_input():
    with pytest.raises(RoomCodeError):
        decode_room("SHORT")
    with pytest.raises(RoomCodeError):
        encode_room("300.1.1.1", 5000)
    with pytest.raises(RoomCodeError):
        encode_room("1.2.3.4", 0)
    with pytest.raises(RoomCodeError):
        format_code("abc")


def test_formatting():
    code = encode_room("127.0.0.1", 51999)
    assert format_code(normalize(code)) == code
