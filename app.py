# -*- coding: utf-8 -*-
"""
netease-ncm2mp3 — 网易云音乐 .ncm 转 MP3 图形工具

把你自己在网易云客户端下载的 .ncm 文件转换成标准 MP3（或保留无损 FLAC）。
"""
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk

import core

APP_NAME = "netease-ncm2mp3"
VERSION = "1.0.0"

BITRATES = [("128 kbps（体积小）", "128k"),
            ("192 kbps（推荐）", "192k"),
            ("320 kbps（体积大）", "320k")]
NAME_TPL = [("歌手 - 歌名", "artist_title"),
            ("歌名 - 歌手", "title_artist"),
            ("沿用原文件名", "origin")]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.files = []                 # [(路径, 状态)]
        self.queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.running = False
        self.ok = self.fail = self.skip = 0

        root.title(f"{APP_NAME} {VERSION}   ·   网易云 .ncm → MP3")
        root.geometry("900x640")
        root.minsize(760, 520)
        self._build_ui()
        self.root.after(100, self._drain_queue)
        self._check_ffmpeg()

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)

        ttk.Button(top, text="添加文件", command=self.add_files).pack(side="left")
        ttk.Button(top, text="添加文件夹", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(top, text="移除选中", command=self.remove_selected).pack(side="left")
        ttk.Button(top, text="清空", command=self.clear).pack(side="left", padx=4)

        self.lbl_count = ttk.Label(top, text="共 0 个文件")
        self.lbl_count.pack(side="right")

        # 文件列表
        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=True, **pad)
        cols = ("name", "status")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=12)
        self.tree.heading("name", text="文件")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=640, anchor="w")
        self.tree.column("status", width=150, anchor="center")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # 设置区
        opt = ttk.LabelFrame(self.root, text="输出设置")
        opt.pack(fill="x", **pad)

        row1 = ttk.Frame(opt)
        row1.pack(fill="x", padx=8, pady=6)
        ttk.Label(row1, text="输出目录：").pack(side="left")
        self.var_outdir = tk.StringVar(value="")
        ttk.Entry(row1, textvariable=self.var_outdir).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row1, text="选择…", command=self.choose_outdir).pack(side="left")
        ttk.Button(row1, text="与源文件同目录", command=lambda: self.var_outdir.set("")).pack(side="left", padx=4)

        row2 = ttk.Frame(opt)
        row2.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(row2, text="输出格式：").pack(side="left")
        self.var_fmt = tk.StringVar(value=BITRATES[1][0])
        ttk.Combobox(row2, textvariable=self.var_fmt, values=[b[0] for b in BITRATES],
                     state="readonly", width=20).pack(side="left", padx=(0, 14))
        ttk.Label(row2, text="命名方式：").pack(side="left")
        self.var_name = tk.StringVar(value=NAME_TPL[0][0])
        ttk.Combobox(row2, textvariable=self.var_name, values=[n[0] for n in NAME_TPL],
                     state="readonly", width=18).pack(side="left", padx=(0, 14))
        ttk.Label(row2, text="并行：").pack(side="left")
        self.var_jobs = tk.IntVar(value=max(2, min(8, (os.cpu_count() or 4))))
        ttk.Spinbox(row2, from_=1, to=16, textvariable=self.var_jobs, width=5).pack(side="left")
        self.var_skip = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="跳过已存在", variable=self.var_skip).pack(side="left", padx=12)

        # 操作区
        act = ttk.Frame(self.root)
        act.pack(fill="x", **pad)
        self.btn_start = ttk.Button(act, text="开始转换", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(act, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(act, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.lbl_stat = ttk.Label(act, text="就绪")
        self.lbl_stat.pack(side="left")

        # 日志
        logf = ttk.LabelFrame(self.root, text="日志")
        logf.pack(fill="both", expand=False, **pad)
        self.log = tk.Text(logf, height=9, state="disabled", wrap="none")
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

    def _check_ffmpeg(self):
        ff = core.find_ffmpeg()
        try:
            r = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                raise OSError
            self.write_log("已找到 ffmpeg：" + ff)
        except Exception:
            self.write_log("⚠ 未找到 ffmpeg，转 MP3 会失败。请把 ffmpeg.exe 放到本程序同目录。")

    # ---------------- 列表操作 ----------------
    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        for p, st in self.files:
            self.tree.insert("", "end", values=(os.path.basename(p), st))
        self.lbl_count.config(text=f"共 {len(self.files)} 个文件")

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择 .ncm 或音频文件",
            filetypes=[("网易云音乐", "*.ncm"), ("音频文件", "*.flac *.mp3 *.m4a *.wav *.aac *.ogg"),
                       ("所有文件", "*.*")])
        self._add(list(paths))

    def add_folder(self):
        d = filedialog.askdirectory(title="选择包含 .ncm 的文件夹")
        if not d:
            return
        found = []
        for root, _, names in os.walk(d):
            for n in names:
                if n.lower().endswith(core.AUDIO_EXTS):
                    found.append(os.path.join(root, n))
        if not found:
            messagebox.showinfo("提示", "该文件夹里没有找到音频文件。")
            return
        self._add(found)

    def _add(self, paths):
        exist = {p for p, _ in self.files}
        for p in paths:
            if p not in exist:
                self.files.append((p, "等待中"))
        self._refresh()

    def remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = sorted((self.tree.index(i) for i in sel), reverse=True)
        for i in idx:
            self.files.pop(i)
        self._refresh()

    def clear(self):
        if self.running:
            return
        self.files.clear()
        self._refresh()

    def choose_outdir(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.var_outdir.set(d)

    # ---------------- 转换 ----------------
    def start(self):
        if self.running or not self.files:
            return
        out_dir = self.var_outdir.get().strip()
        if out_dir and not os.path.isdir(out_dir):
            messagebox.showerror("错误", "输出目录不存在。")
            return

        bitrate = dict(BITRATES)[self.var_fmt.get()]
        naming = dict(NAME_TPL)[self.var_name.get()]
        jobs = max(1, int(self.var_jobs.get()))
        skip = self.var_skip.get()

        self.running = True
        self.stop_flag.clear()
        self.ok = self.fail = self.skip = 0
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.progress["value"] = 0

        todo = list(enumerate(self.files))
        threading.Thread(target=self._worker, args=(todo, out_dir, bitrate, naming, jobs, skip),
                         daemon=True).start()

    def stop(self):
        self.stop_flag.set()
        self.write_log("正在停止…")

    def _worker(self, todo, out_dir, bitrate, naming, jobs, skip):
        t0 = time.time()
        done = 0
        total = len(todo)

        def one(item):
            idx, (path, _status) = item
            if self.stop_flag.is_set():
                return idx, path, "已取消", None
            try:
                self.queue.put(("status", idx, "解密中…"))
                tmpd = tempfile.mkdtemp(prefix="ncm2mp3_")
                raw = os.path.join(tmpd, "raw.bin")
                meta = {}
                if path.lower().endswith(".ncm"):
                    meta = core.decrypt_ncm(path, raw)
                    fmt = core.sniff_audio(open(raw, "rb").read(64))
                else:
                    raw = path
                    fmt = core.sniff_audio(open(path, "rb").read(64))
                if fmt == "unknown":
                    raise ValueError("无法识别的音频格式")

                title = meta.get("musicName") or ""
                artists = meta.get("artist") or []
                if isinstance(artists, list):
                    artist = "、".join(a[0] if isinstance(a, (list, tuple)) and a else str(a)
                                      for a in artists)
                else:
                    artist = str(artists)
                if not title:
                    stem = os.path.splitext(os.path.basename(path))[0]
                    parts = stem.split(" - ", 1)
                    if len(parts) == 2:
                        artist = artist or parts[0]
                        title = parts[1]
                    else:
                        title = stem

                if naming == "artist_title" and artist:
                    base = f"{artist} - {title}"
                elif naming == "title_artist" and artist:
                    base = f"{title} - {artist}"
                else:
                    base = os.path.splitext(os.path.basename(path))[0]
                base = core.safe_filename(base)

                target_dir = out_dir or os.path.dirname(path)
                os.makedirs(target_dir, exist_ok=True)
                out_path = os.path.join(target_dir, base + ".mp3")
                if skip and os.path.exists(out_path):
                    return idx, path, "已存在，跳过", None
                k = 2
                while os.path.exists(out_path) and not skip:
                    out_path = os.path.join(target_dir, f"{base} ({k}).mp3")
                    k += 1

                self.queue.put(("status", idx, "转码中…"))
                ok, err = core.encode(raw, out_path, meta, bitrate)
                if raw != path and os.path.exists(raw):
                    try:
                        os.remove(raw)
                    except OSError:
                        pass
                try:
                    os.rmdir(tmpd)
                except Exception:
                    pass
                if not ok:
                    raise RuntimeError(err or "ffmpeg 转码失败")
                return idx, path, f"完成 · {os.path.getsize(out_path)/1048576:.1f} MB", None
            except Exception as e:
                return idx, path, "失败：" + str(e), str(e)

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for idx, path, status, err in pool.map(one, todo):
                done += 1
                if status.startswith("完成"):
                    self.ok += 1
                elif status.startswith("已存在"):
                    self.skip += 1
                elif status.startswith("已取消"):
                    pass
                else:
                    self.fail += 1
                self.queue.put(("done", idx, status))
                self.queue.put(("progress", done * 100 // max(1, total)))
                if self.stop_flag.is_set():
                    pass

        elapsed = time.time() - t0
        self.queue.put(("finish", self.ok, self.fail, self.skip, elapsed))

    # ---------------- 队列 ----------------
    def write_log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _drain_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "status":
                    _, idx, st = msg
                    if idx < len(self.tree.get_children()):
                        iid = self.tree.get_children()[idx]
                        self.tree.set(iid, "status", st)
                elif kind == "done":
                    _, idx, st = msg
                    if idx < len(self.tree.get_children()):
                        iid = self.tree.get_children()[idx]
                        self.tree.set(iid, "status", st)
                    name = os.path.basename(self.files[idx][0]) if idx < len(self.files) else ""
                    self.write_log(f"[{st}] {name}")
                elif kind == "progress":
                    self.progress["value"] = msg[1]
                elif kind == "finish":
                    _, ok, fail, skip, elapsed = msg
                    self.running = False
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.progress["value"] = 100
                    self.lbl_stat.config(text=f"成功 {ok} · 跳过 {skip} · 失败 {fail}")
                    self.write_log(f"—— 完成：成功 {ok}，跳过 {skip}，失败 {fail}，用时 {elapsed:.1f}s ——")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
