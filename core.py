# -*- coding: utf-8 -*-
"""
网易云音乐 .ncm 解密与格式转换核心。

只做「格式转换」：把你自己在客户端下载的 .ncm 还原成标准音频文件，
再交给 ffmpeg 转成 MP3 / 保留无损。
"""
import base64
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading

from Crypto.Cipher import AES

MAGIC = b"CTENFDAM"
CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")
META_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")
KEYBOX_PERIOD = 0x8000          # 数据按 32 KB 分块读取
KS_PERIOD = 256                 # 密钥流的真实周期（j = (i+1) & 0xFF）
AUDIO_EXTS = (".ncm", ".flac", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".wma")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if 0 < n <= 16:
        return data[:-n]
    return data


def _aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    return AES.new(key, AES.MODE_ECB).decrypt(data)


def _xor_fast(data: bytes, keystream: bytes) -> bytes:
    """
    用大整数异或实现高速逐字节 XOR（纯 Python 也能到 GB/s 级别）。
    keystream 会按需重复到与 data 等长。
    """
    n = len(data)
    if n == 0:
        return b""
    k = (keystream * (n // len(keystream) + 1))[:n]
    return (int.from_bytes(data, "little") ^ int.from_bytes(k, "little")).to_bytes(n, "little")


def sniff_audio(raw: bytes) -> str:
    """根据文件头判断真实格式。"""
    if raw[:4] == b"fLaC":
        return "flac"
    if raw[:3] == b"ID3" or raw[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        return "mp3"
    if raw[:4] == b"OggS":
        return "ogg"
    if raw[:4] == b"RIFF":
        return "wav"
    if raw[:4] in (b"\x30\x26\xb2\x75", b"\x00\x00\x00\x20"[:4]):
        return "m4a"
    return "unknown"


_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name: str, fallback: str = "未命名") -> str:
    name = _ILLEGAL.sub("_", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or fallback)[:150]


# --------------------------------------------------------------------------
# NCM 解密
# --------------------------------------------------------------------------
def _read_meta(f, length: int) -> dict:
    raw = bytearray(f.read(length))
    for i in range(length):
        raw[i] ^= 0x63
    text = bytes(raw).decode("utf-8", "ignore")
    if text.startswith("163 key(Don't modify):"):
        text = text[22:]
    try:
        plain = _unpad(_aes_ecb_decrypt(META_KEY, base64.b64decode(text)))
        payload = plain[6:] if plain.startswith(b"music:") else plain
        return json.loads(payload.decode("utf-8", "ignore"))
    except Exception:
        return {}


def _build_keybox(key_data: bytes) -> bytearray:
    box = bytearray(range(256))
    c = last = 0
    off = 0
    n = len(key_data)
    for i in range(256):
        swap = box[i]
        c = (swap + last + key_data[off]) & 0xFF
        off += 1
        if off >= n:
            off = 0
        box[i] = box[c]
        box[c] = swap
        last = c
    return box


def _keystream(box: bytearray, length: int = KS_PERIOD) -> bytes:
    out = bytearray(length)
    for i in range(length):
        j = (i + 1) & 0xFF
        out[i] = box[(box[j] + box[(box[j] + j) & 0xFF]) & 0xFF]
    return bytes(out)


def decrypt_ncm(path: str, out_raw: str, progress=None, cover_path: str = None) -> dict:
    """
    把 .ncm 解密成原始音频文件（FLAC / MP3），返回元数据字典。

    cover_path: 若给出路径，则把内嵌封面图写出来（自动补 .jpg/.png 后缀由调用方决定）。
    """
    with open(path, "rb") as f:
        if f.read(8) != MAGIC:
            raise ValueError("不是有效的 .ncm 文件")

        f.read(2)                                   # 2 字节间隔
        key_len = struct.unpack("<I", f.read(4))[0]
        key_data = bytearray(f.read(key_len))
        for i in range(key_len):
            key_data[i] ^= 0x64
        key_data = _unpad(_aes_ecb_decrypt(CORE_KEY, bytes(key_data)))
        if key_data.startswith(b"neteasecloudmusic"):
            key_data = key_data[17:]
        if not key_data:
            raise ValueError("密钥解析失败，文件可能已损坏")

        box = _build_keybox(key_data)
        ks = _keystream(box)

        meta_len = struct.unpack("<I", f.read(4))[0]
        meta = _read_meta(f, meta_len) if meta_len else {}

        f.read(4)                                   # CRC
        f.read(5)                                   # 间隔
        img_len = struct.unpack("<I", f.read(4))[0]
        if img_len:
            img = f.read(img_len)
            if cover_path:
                try:
                    with open(cover_path, "wb") as cf:
                        cf.write(img)
                    meta["_cover"] = cover_path
                except OSError:
                    pass

        total = os.path.getsize(path)
        with open(out_raw, "wb") as out:
            while True:
                chunk = f.read(KEYBOX_PERIOD)
                if not chunk:
                    break
                out.write(_xor_fast(chunk, ks))
                if progress and total:
                    progress(min(f.tell(), total) / total)
    return meta


# --------------------------------------------------------------------------
# ffmpeg 封装
# --------------------------------------------------------------------------
def find_ffmpeg() -> str:
    """依次在：程序所在目录 / bin 子目录 / 打包临时目录 / 当前目录 / PATH 里找 ffmpeg。"""
    names = ["ffmpeg.exe", "ffmpeg"] if os.name == "nt" else ["ffmpeg"]
    dirs = []
    for base in (os.path.dirname(os.path.abspath(__file__)),
                 getattr(sys, "_MEIPASS", None),
                 os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else None,
                 os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else None,
                 os.getcwd()):
        if not base:
            continue
        dirs.append(base)
        dirs.append(os.path.join(base, "bin"))
    seen = set()
    for d in dirs:
        if d in seen:
            continue
        seen.add(d)
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return "ffmpeg"          # 交给 PATH


def encode(raw_path: str, out_path: str, meta: dict, bitrate: str = "192k",
           keep_lossless: bool = False, progress=None) -> tuple:
    """把解密出的原始音频转成目标格式。返回 (成功?, 错误信息)"""
    if keep_lossless:
        if os.path.splitext(raw_path)[1].lower() == os.path.splitext(out_path)[1].lower():
            os.replace(raw_path, out_path)
            return True, ""
    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           "-i", raw_path, "-vn", "-map_metadata", "-1"]
    if not keep_lossless:
        cmd += ["-c:a", "libmp3lame", "-b:a", bitrate, "-ar", "44100"]
    if meta.get("title"):
        cmd += ["-metadata", "title=" + str(meta["title"])]
    if meta.get("artist"):
        cmd += ["-metadata", "artist=" + str(meta["artist"])]
    if meta.get("album"):
        cmd += ["-metadata", "album=" + str(meta["album"])]
    cmd += ["-id3v2_version", "3", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return False, (r.stderr or "").strip()[-400:]
    return True, ""
