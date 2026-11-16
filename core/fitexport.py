"""Génération de fichiers FIT « workout » pour compteurs (Garmin/Wahoo).

Steps : durée en secondes (time) ou en mètres (distance), cible en watts.
Le fichier est un FIT workout valide (file_id + workout + workout_step),
vérifiable au retour par fitdecode.
"""
import io
import struct
from datetime import datetime

FIT_EPOCH = datetime(1989, 12, 31)

BASE_ENUM = 0x00
BASE_STRING = 0x07
BASE_UINT16 = 0x84
BASE_UINT32 = 0x86

MESG_FILE_ID = 0
MESG_WORKOUT = 26
MESG_WORKOUT_STEP = 27

DUR_TIME = 0
DUR_DISTANCE = 1
TARGET_POWER = 4


_CRC_TABLE = (
    0x0000, 0xcc01, 0xd801, 0x1400, 0xf001, 0x3c00, 0x2800, 0xe401,
    0xa001, 0x6c00, 0x7800, 0xb401, 0x5000, 0x9c01, 0x8801, 0x4400)


def _crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc ^= tmp ^ _CRC_TABLE[b & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc ^= tmp ^ _CRC_TABLE[(b >> 4) & 0xF]
    return crc


_SIZES = {BASE_ENUM: 1, BASE_UINT16: 2, BASE_UINT32: 4, BASE_STRING: 16}


def _def_message(local: int, mesg_num: int, fields: list) -> bytes:
    out = bytearray([0x40 | local, 0x00, 0x00])
    out += struct.pack("<H", mesg_num)
    out.append(len(fields))
    for def_num, base_type in fields:
        out += bytes([def_num, _SIZES[base_type], base_type])
    return bytes(out)


def _data_message(local: int, fields: list, values: list) -> bytes:
    out = bytearray([0x00 | local])
    for (_, base_type), v in zip(fields, values):
        if base_type == BASE_STRING:
            out += v.encode("ascii", "replace")[: 15].ljust(16, b"\x00")
        elif base_type == BASE_UINT16:
            out += struct.pack("<H", v & 0xFFFF)
        elif base_type == BASE_UINT32:
            out += struct.pack("<I", v & 0xFFFFFFFF)
        else:
            out += bytes([v & 0xFF])
    return bytes(out)


def build_workout_fit(name: str, steps: list) -> bytes:
    """steps : [{name, seconds} ou {name, meters}, watts].

    watts = 0 → étape sans cible (open).
    """
    ts = int((datetime.utcnow() - FIT_EPOCH).total_seconds())
    body = bytearray()

    fid_fields = [(0, BASE_ENUM), (1, BASE_UINT16), (2, BASE_UINT16),
                  (3, BASE_UINT32), (4, BASE_UINT32)]
    body += _def_message(0, MESG_FILE_ID, fid_fields)
    body += _data_message(0, fid_fields, [5, 255, 0, 0x57544831, ts])

    n = len(steps)
    wkt_fields = [(4, BASE_ENUM), (6, BASE_UINT16), (8, BASE_STRING)]
    body += _def_message(1, MESG_WORKOUT, wkt_fields)
    body += _data_message(1, wkt_fields, [2, n, name])

    step_fields = [(0, BASE_STRING), (1, BASE_ENUM), (2, BASE_UINT32),
                   (3, BASE_ENUM), (4, BASE_UINT32), (254, BASE_UINT16)]
    body += _def_message(2, MESG_WORKOUT_STEP, step_fields)
    for i, s in enumerate(steps):
        if "seconds" in s:
            dur_type, dur_val = DUR_TIME, int(s["seconds"])
        else:
            dur_type, dur_val = DUR_DISTANCE, int(round(s["meters"] * 100))
        watts = int(s.get("watts") or 0)
        body += _data_message(2, step_fields,
                              [s.get("name", f"Step {i + 1}"), dur_type,
                               dur_val, TARGET_POWER if watts else 0, watts, i])

    data = bytes(body)
    header = bytearray([0x0E, 0x10, 0x20, 0x14])
    header += struct.pack("<I", len(data))
    header += b".FIT"
    header += struct.pack("<H", _crc16(bytes(header)))
    crc = struct.pack("<H", _crc16(header + data))
    return bytes(header) + data + crc


def workout_from_blocks(blocks: list, name: str) -> bytes:
    """blocks : [(label, seconds, watts)] → FIT workout (échauffement)."""
    steps = [{"name": lbl, "seconds": sec, "watts": w} for lbl, sec, w in blocks
             if sec > 0]
    return build_workout_fit(name, steps)


def workout_from_parts(parts: list, name: str) -> bytes:
    """parts : [{n, dist_km, watts, time_s}] → FIT workout (plan de course)."""
    steps = [{"name": f"P{p['n']} {p['watts']}W", "meters": p["dist_km"] * 1000.0,
              "watts": p["watts"]} for p in parts if p["dist_km"] > 0]
    return build_workout_fit(name, steps)
