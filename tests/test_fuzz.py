import json
import os
import random
import struct

from core import chunker, image_payload, material, object_payload, protocol


def test_random_binary_decoders_do_not_crash():
    rng = random.Random(7)
    payloads = [bytes(rng.randrange(256) for _ in range(size)) for size in (0, 1, 16, 64, 1024)]
    for payload in payloads:
        for decoder in (
            protocol.decode,
            chunker.decode_positions,
            chunker.decode_delta,
            chunker.decode_geometry,
            chunker.unpack_geometry,
            image_payload.decode,
            object_payload.decode_create,
        ):
            try:
                decoder(payload)
            except Exception:  # noqa: BLE001 - rejection is the expected outcome
                pass


def test_material_sanitize_survives_random_json():
    rng = random.Random(11)
    keys = ["name", "base_color", "metallic", "roughness", "nodes", "links", "image", "props", "inputs"]
    for _ in range(200):
        value = {}
        for _ in range(rng.randrange(0, 6)):
            key = rng.choice(keys)
            value[key] = rng.choice(
                [None, True, 1, -5, 1e9, "text", [1, 2], {"x": 1}, [{"bl_idname": "ShaderNodeScript"}]]
            )
        try:
            material.sanitize(value)
        except Exception:  # noqa: BLE001
            pass


def test_protocol_rejects_bad_headers():
    assert protocol.PROTOCOL_VERSION >= 2
    for payload in (b"", b"BMP1", b"X" * 20, struct.pack("<4sBBBBIII", b"BMP1", 9, 1, 0, 0, 0, 0, 0)):
        try:
            protocol.decode(payload)
        except ValueError:
            pass
        else:
            raise AssertionError("bad frame was accepted")
