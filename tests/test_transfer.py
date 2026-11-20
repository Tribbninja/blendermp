import os

import pytest

from core import transfer


def test_split_and_reassemble_roundtrip(monkeypatch):
    monkeypatch.setattr(transfer, "CHUNK_SIZE", 64)
    payload = bytes(range(256)) * 10
    identity, chunks = transfer.split(payload, transfer.KIND_OBJECT)
    assert identity == transfer.transfer_id(payload)
    assert len(chunks) > 1
    reassembler = transfer.Reassembler()
    result = None
    for chunk in chunks:
        result, progress = reassembler.add("host", chunk, now=1.0)
        assert progress["total"] == len(chunks)
    assert result is not None
    assert result["kind"] == transfer.KIND_OBJECT
    assert result["data"] == payload
    assert reassembler.pending() == 0


def test_duplicate_chunks_are_idempotent(monkeypatch):
    monkeypatch.setattr(transfer, "CHUNK_SIZE", 32)
    payload = os.urandom(100)
    _identity, chunks = transfer.split(payload)
    reassembler = transfer.Reassembler()
    reassembler.add("host", chunks[0], now=1.0)
    reassembler.add("host", chunks[0], now=1.0)
    for chunk in chunks[1:]:
        result, _progress = reassembler.add("host", chunk, now=1.0)
    assert result["data"] == payload


def test_out_of_order_chunks_are_rejected(monkeypatch):
    monkeypatch.setattr(transfer, "CHUNK_SIZE", 32)
    payload = os.urandom(100)
    _identity, chunks = transfer.split(payload)
    with pytest.raises(transfer.TransferError):
        transfer.Reassembler().add("host", chunks[1], now=1.0)


def test_missing_chunks_survive_until_completed(monkeypatch):
    monkeypatch.setattr(transfer, "CHUNK_SIZE", 32)
    payload = os.urandom(100)
    _identity, chunks = transfer.split(payload)
    reassembler = transfer.Reassembler(ttl=10.0)
    reassembler.add("host", chunks[0], now=1.0)
    result, progress = reassembler.add("host", chunks[2], now=2.0)
    assert result is None
    assert progress["received"] == 2
    assert reassembler.pending() == 1
    assert reassembler.sweep(now=20.0) == 1


def test_decode_rejects_tampering(monkeypatch):
    monkeypatch.setattr(transfer, "CHUNK_SIZE", 32)
    _identity, chunks = transfer.split(os.urandom(100))
    bad = bytearray(chunks[0])
    bad[0] = ord("X")
    with pytest.raises(transfer.TransferError):
        transfer.decode_chunk(bytes(bad))
    with pytest.raises(transfer.TransferError):
        transfer.decode_chunk(chunks[0][:-1])


def test_transfer_limits(monkeypatch):
    monkeypatch.setattr(transfer, "MAX_TRANSFER", 64)
    with pytest.raises(transfer.TransferError):
        transfer.split(os.urandom(65))
    with pytest.raises(transfer.TransferError):
        transfer.split(b"")
