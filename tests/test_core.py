# -*- coding: utf-8 -*-
"""
core.py 的单元测试。

不需要任何真实 .ncm 文件：测试里用同样的算法**构造**一个 ncm，
再交给解密器还原，验证能从封装里无损取回原始音频与元数据。
"""
import base64
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Crypto.Cipher import AES          # noqa: E402

import core                            # noqa: E402


def _pad(data: bytes) -> bytes:
    n = 16 - (len(data) % 16)
    return data + bytes([n]) * n


def build_ncm(audio: bytes, meta: dict, cover: bytes = b"", keybytes: bytes = None) -> bytes:
    """按 ncm 的封装格式拼一个合法的 .ncm 文件。"""
    keybytes = keybytes or bytes(range(98))
    plain = b"neteasecloudmusic" + keybytes
    enc_key = AES.new(core.CORE_KEY, AES.MODE_ECB).encrypt(_pad(plain))
    key_blob = bytes(b ^ 0x64 for b in enc_key)

    meta_text = "music:" + json.dumps(meta, ensure_ascii=False)
    enc_meta = AES.new(core.META_KEY, AES.MODE_ECB).encrypt(_pad(meta_text.encode("utf-8")))
    meta_plain = "163 key(Don't modify):" + base64.b64encode(enc_meta).decode()
    meta_raw = bytes(b ^ 0x63 for b in meta_plain.encode("utf-8"))

    ks = core._keystream(core._build_keybox(keybytes))
    enc_audio = core._xor_fast(audio, ks)

    out = bytearray()
    out += core.MAGIC
    out += b"\x01\x70"
    out += struct.pack("<I", len(key_blob)) + key_blob
    out += struct.pack("<I", len(meta_raw)) + meta_raw
    out += b"\x00\x00\x00\x00"          # CRC
    out += b"\x00" * 5                  # 间隔
    out += struct.pack("<I", len(cover)) + cover
    out += enc_audio
    return bytes(out)


FAKE_FLAC = b"fLaC" + bytes(range(256)) * 400        # 约 100 KB，分多个 32KB 块
FAKE_MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00\x01" * 50000
META = {"musicName": "测试歌曲", "artist": [["测试歌手", 1]],
        "album": "测试专辑", "format": "flac"}


class TestHelpers(unittest.TestCase):
    def test_xor_matches_naive(self):
        data = bytes(range(256)) * 5
        ks = bytes((i * 7) & 0xFF for i in range(64))
        expect = bytes(d ^ ks[i % len(ks)] for i, d in enumerate(data))
        self.assertEqual(core._xor_fast(data, ks), expect)

    def test_xor_empty(self):
        self.assertEqual(core._xor_fast(b"", b"abc"), b"")

    def test_keystream_period(self):
        ks = core._keystream(core._build_keybox(bytes(range(98))))
        self.assertEqual(len(ks), core.KS_PERIOD)
        # 密钥流以 256 字节为周期
        big = core._keystream(core._build_keybox(bytes(range(98))), 512)
        self.assertEqual(big[:256], big[256:])

    def test_sniff(self):
        self.assertEqual(core.sniff_audio(FAKE_FLAC), "flac")
        self.assertEqual(core.sniff_audio(FAKE_MP3), "mp3")
        self.assertEqual(core.sniff_audio(b"OggS...."), "ogg")
        self.assertEqual(core.sniff_audio(b"RIFF...."), "wav")
        self.assertEqual(core.sniff_audio(b"????"), "unknown")

    def test_safe_filename(self):
        self.assertEqual(core.safe_filename('a/b:c*d?e"f<g>h|i'), "a_b_c_d_e_f_g_h_i")
        self.assertEqual(core.safe_filename("  ..名字..  "), "名字")
        self.assertEqual(core.safe_filename(""), "未命名")
        self.assertEqual(len(core.safe_filename("x" * 400)), 150)

    def test_unpad(self):
        self.assertEqual(core._unpad(b"abc" + bytes([13]) * 13), b"abc")
        self.assertEqual(core._unpad(b"abc"), b"abc")
        self.assertEqual(core._unpad(b""), b"")


class TestDecrypt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ncm_test_")

    def tearDown(self):
        for f in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, f))
        os.rmdir(self.tmp)

    def _roundtrip(self, audio, cover=b""):
        ncm = os.path.join(self.tmp, "sample.ncm")
        raw = os.path.join(self.tmp, "raw.bin")
        cvr = os.path.join(self.tmp, "cover.jpg") if cover else None
        with open(ncm, "wb") as f:
            f.write(build_ncm(audio, META, cover))
        meta = core.decrypt_ncm(ncm, raw, cover_path=cvr)
        with open(raw, "rb") as f:
            got = f.read()
        return meta, got, cvr

    def test_flac_roundtrip(self):
        meta, got, _ = self._roundtrip(FAKE_FLAC)
        self.assertEqual(got, FAKE_FLAC)
        self.assertEqual(meta["musicName"], "测试歌曲")
        self.assertEqual(meta["album"], "测试专辑")
        self.assertEqual(core.sniff_audio(got), "flac")

    def test_mp3_roundtrip(self):
        meta, got, _ = self._roundtrip(FAKE_MP3)
        self.assertEqual(got, FAKE_MP3)
        self.assertEqual(core.sniff_audio(got), "mp3")

    def test_cover_extracted(self):
        cover = b"\x89PNG\r\n\x1a\n" + b"x" * 500
        meta, got, cvr = self._roundtrip(FAKE_FLAC, cover)
        self.assertEqual(got, FAKE_FLAC)
        self.assertTrue(os.path.exists(cvr))
        with open(cvr, "rb") as f:
            self.assertEqual(f.read(), cover)

    def test_bad_magic(self):
        bad = os.path.join(self.tmp, "bad.ncm")
        with open(bad, "wb") as f:
            f.write(b"NOTANCM!" + b"\x00" * 100)
        with self.assertRaises(ValueError):
            core.decrypt_ncm(bad, os.path.join(self.tmp, "x.bin"))

    def test_progress_called(self):
        seen = []
        ncm = os.path.join(self.tmp, "s.ncm")
        with open(ncm, "wb") as f:
            f.write(build_ncm(FAKE_FLAC, META))
        core.decrypt_ncm(ncm, os.path.join(self.tmp, "r.bin"), progress=seen.append)
        self.assertTrue(seen)
        self.assertGreaterEqual(seen[-1], 0.99)


if __name__ == "__main__":
    unittest.main(verbosity=2)
