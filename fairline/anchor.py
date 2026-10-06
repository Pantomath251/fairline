"""Decode Anchor events and instructions from Solana transaction logs / data using an Anchor IDL (spec 0.1.0).

Anchor emits events as `Program data: <base64>` log lines: 8-byte discriminator followed by the Borsh-encoded
struct. Instruction data has the same layout (discriminator + Borsh args)."""
from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import base58


class Reader:
    def __init__(self, b: bytes):
        self.b, self.i = b, 0

    def take(self, n: int) -> bytes:
        if self.i + n > len(self.b):
            raise ValueError("buffer underrun")
        v = self.b[self.i:self.i + n]
        self.i += n
        return v

    def u(self, n: int) -> int:
        return int.from_bytes(self.take(n), "little", signed=False)

    def s(self, n: int) -> int:
        return int.from_bytes(self.take(n), "little", signed=True)


_INTS = {"u8": (1, False), "u16": (2, False), "u32": (4, False), "u64": (8, False), "u128": (16, False),
         "i8": (1, True), "i16": (2, True), "i32": (4, True), "i64": (8, True), "i128": (16, True)}


class Idl:
    def __init__(self, idl: dict):
        self.idl = idl
        self.address = idl.get("address")
        self.types = {t["name"]: t for t in idl.get("types") or []}
        self.events = {bytes(e["discriminator"]): e["name"] for e in idl.get("events") or []}
        self.instructions = {bytes(ix["discriminator"]): ix for ix in idl.get("instructions") or []}

    @classmethod
    def load(cls, path: str | Path) -> "Idl":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- generic borsh ----------------------------------------------------------------------------------------
    def read(self, r: Reader, t):
        if isinstance(t, str):
            if t in _INTS:
                n, signed = _INTS[t]
                v = r.s(n) if signed else r.u(n)
                return str(v) if n > 8 else v  # keep u128/i128 exact for JSON consumers
            if t == "bool":
                return r.u(1) != 0
            if t == "pubkey":
                return base58.b58encode(r.take(32)).decode()
            if t == "string":
                return r.take(r.u(4)).decode("utf-8", "replace")
            if t == "bytes":
                return r.take(r.u(4)).hex()
            if t == "f32":
                return struct.unpack("<f", r.take(4))[0]
            if t == "f64":
                return struct.unpack("<d", r.take(8))[0]
            raise ValueError(f"unsupported type {t}")
        if "option" in t:
            return self.read(r, t["option"]) if r.u(1) else None
        if "vec" in t:
            return [self.read(r, t["vec"]) for _ in range(r.u(4))]
        if "array" in t:
            inner, n = t["array"]
            return [self.read(r, inner) for _ in range(n)]
        if "defined" in t:
            name = t["defined"]["name"] if isinstance(t["defined"], dict) else t["defined"]
            return self.read_defined(r, name)
        raise ValueError(f"unsupported type {t}")

    def read_defined(self, r: Reader, name: str):
        td = self.types[name]["type"]
        if td["kind"] == "struct":
            return {f["name"]: self.read(r, f["type"]) for f in td.get("fields") or []}
        if td["kind"] == "enum":
            idx = r.u(1)
            v = td["variants"][idx]
            if v.get("fields"):
                fields = v["fields"]
                if isinstance(fields[0], dict):
                    return {v["name"]: {f["name"]: self.read(r, f["type"]) for f in fields}}
                return {v["name"]: [self.read(r, f) for f in fields]}
            return v["name"]
        raise ValueError(f"unsupported kind {td['kind']}")

    # ---- events / instructions ---------------------------------------------------------------------------------
    def decode_event(self, data: bytes) -> dict | None:
        name = self.events.get(data[:8])
        if not name:
            return None
        try:
            return {"name": name, "data": self.read_defined(Reader(data[8:]), name)}
        except (ValueError, KeyError, IndexError):
            return {"name": name, "data": None}

    def decode_logs(self, logs: list[str]) -> list[dict]:
        """Events emitted by this program in a transaction's logMessages (tracks the invoke stack so data logged
        by other programs is ignored)."""
        out, stack = [], []
        for line in logs or []:
            if line.startswith("Program ") and " invoke [" in line:
                stack.append(line.split()[1])
            elif line.startswith("Program ") and (line.endswith(" success") or " failed" in line):
                if stack:
                    stack.pop()
            elif line.startswith("Program data: ") and (not stack or stack[-1] == self.address):
                try:
                    ev = self.decode_event(base64.b64decode(line[len("Program data: "):]))
                except (ValueError, base64.binascii.Error):
                    ev = None
                if ev:
                    out.append(ev)
        return out

    def instruction_names(self, logs: list[str]) -> list[str]:
        """Anchor logs `Program log: Instruction: CreateEvent` for every handler it enters."""
        return [ln.split("Instruction: ", 1)[1] for ln in logs or [] if ln.startswith("Program log: Instruction: ")]

    def decode_instruction(self, data: bytes) -> dict | None:
        ix = self.instructions.get(data[:8])
        if not ix:
            return None
        r = Reader(data[8:])
        try:
            args = {a["name"]: self.read(r, a["type"]) for a in ix.get("args") or []}
        except (ValueError, KeyError, IndexError):
            args = None
        return {"name": ix["name"], "args": args}


IDL_DIR = Path(__file__).resolve().parent / "idl"


def load_all() -> dict[str, Idl]:
    out = {}
    for p in sorted(IDL_DIR.glob("*.json")):
        idl = Idl.load(p)
        if idl.address:
            out[idl.address] = idl
    return out
