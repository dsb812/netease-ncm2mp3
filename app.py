# -*- coding: utf-8 -*-
"""
netease-ncm2mp3 — 网易云音乐 .ncm 转 MP3 图形工具

把你自己在网易云客户端下载的 .ncm 文件转换成标准 MP3（或保留无损 FLAC）。

用法：
    python app.py                      # 打开图形界面
    python app.py --cli a.ncm b.ncm    # 命令行批量转换
    python app.py --cli 某文件夹 --out 输出目录 --bitrate 320k
"""
import argparse
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
VERSION = "1.1.0"

BITRATES = [("128 kbps（体积小）", "128k"),
            ("192 kbps（推荐）", "192k"),
            ("320 kbps（体积大）", "320k")]
NAME_TPL = [("歌手 - 歌名", "artist_title"),
            ("歌名 - 歌手", "title_artist"),
            ("沿用原文件名", "origin")]


# ---------------------------------------------------------------------------
# 共用：单个文件转换
# ---------------------------------------------------------------------------
def collect_inputs(paths, recursive=True):
    """把「文件 / 文件夹」混合列表展开成待处理文件列表。"""
    out = []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            if recursive:
                for root, _, names in os.walk(p):
                    for n in names:
                        if n.lower().endswith(core.AUDIO_EXTS) and not n.startswith("~"):
                            out.append(os.path.join(root, n))
            else:
                for n in os.listdir(p):
                    fp = os.path.join(p, n)
                    if os.path.isfile(fp) and n.lower().endswith(core.AUDIO_EXTS):
                        out.append(fp)
        elif os.path.isfile(p):
            out.append(p)
    seen, uniq = set(), []
    for p in out:
        k = os.path.normcase(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def parse_meta(meta, path):
    """从 ncm 元数据里取标题/歌手/专辑，缺失则从文件名推断。"""
    title = meta.get("musicName") or ""
    artists = meta.get("artist") or []
    if isinstance(artists, list):
        artist = "、".join(a[0] if isinstance(a, (list, tuple)) and a else str(a) for a in artists)
    else:
        artist = str(artists)
    album = meta.get("album") or ""
    if not title:
        stem = os.path.splitext(os.path.basename(path))[0]
        parts = stem.split(" - ", 1)
        if len(parts) == 2:
            artist = artist or parts[0]
            title = parts[1]
        else:
            title = stem
    return title, artist, album


def build_target_name(path, title, artist, naming):
    if naming == "artist_title" and artist:
        base = f"{artist} - {title}"
    elif naming == "title_artist" and artist:
        base = f"{title} - {artist}"
    else:
        base = os.path.splitext(os.path.basename(path))[0]
    return core.safe_filename(base)


def convert_file(path, out_dir=None, bitrate="192k", naming="artist_title",
                 skip_existing=True, cover=False, embed_cover=False, progress=None):
    """
    转换单个文件。返回 (状态, 信息)：状态 ∈ {"ok", "skip", "fail"}。
    """
    tmpd = None
    try:
        raw = path
        meta = {}
        cover_path = None
        if path.lower().endswith(".ncm"):
            tmpd = tempfile.mkdtemp(prefix="ncm2mp3_")
            raw = os.path.join(tmpd, "raw.bin")
            if cover or embed_cover:
                cover_path = os.path.join(tmpd, "cover.bin")
            meta = core.decrypt_ncm(path, raw, progress=progress, cover_path=cover_path)
            fmt = core.sniff_audio(open(raw, "rb").read(64))
        else:
            fmt = core.sniff_audio(open(path, "rb").read(64))

        if fmt == "unknown":
            return "fail", "无法识别的音频格式"

        keep_lossless = (bitrate == "lossless")
        title, artist, album = parse_meta(meta, path)
        base = build_target_name(path, title, artist, naming)

        target_dir = out_dir or os.path.dirname(path)
        os.makedirs(target_dir, exist_ok=True)
        ext = ".flac" if keep_lossless else ".mp3"
        out_path = os.path.join(target_dir, base + ext)
        if skip_existing and os.path.exists(out_path):
            return "skip", "已存在，跳过"
        k = 2
        while os.path.exists(out_path) and not skip_existing:
            out_path = os.path.join(target_dir, f"{base} ({k}){ext}")
            k += 1

        ok, err = core.encode(raw, out_path,
                              {"title": title, "artist": artist, "album": album},
                              bitrate="192k" if keep_lossless else bitrate,
                              keep_lossless=keep_lossless,
                              cover_path=cover_path if embed_cover else None)
        if not ok:
            return "fail", err or "ffmpeg 转码失败"

        # 封面另存到输出目录
        if cover and cover_path and os.path.exists(cover_path):
            with open(cover_path, "rb") as f:
                head = f.read(8)
            cext = ".png" if head.startswith(b"\x89PNG") else ".jpg"
            try:
                os.replace(cover_path, os.path.join(target_dir, base + cext))
            except OSError:
                pass

        size = os.path.getsize(out_path) / 1048576
        return "ok", f"完成 · {size:.1f} MB"
    except Exception as e:
        return "fail", str(e)
    finally:
        if tmpd:
            for n in os.listdir(tmpd):
                try:
                    os.remove(os.path.join(tmpd, n))
                except OSError:
                    pass
            try:
                os.rmdir(tmpd)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def cli_main(argv):
    try:                      # Windows 控制台默认 GBK，中文/符号会炸，统一改 UTF-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        prog=APP_NAME,
        description="网易云 .ncm 转 MP3（批量）")
    ap.add_argument("inputs", nargs="+", help="文件或文件夹")
    ap.add_argument("-o", "--out", default=None, help="输出目录（默认与源文件同目录）")
    ap.add_argument("-b", "--bitrate", default="192k",
                    choices=["128k", "192k", "320k", "lossless"], help="输出码率；lossless=保留 FLAC")
    ap.add_argument("-j", "--jobs", type=int, default=max(2, min(8, os.cpu_count() or 4)),
                    help="并行数")
    ap.add_argument("-n", "--naming", default="artist_title",
                    choices=[n[1] for n in NAME_TPL], help="命名方式")
    ap.add_argument("--no-skip", action="store_true", help="不跳过已存在的文件")
    ap.add_argument("--cover", action="store_true", help="同时导出封面图")
    ap.add_argument("--embed-cover", action="store_true", help="把封面嵌入 MP3")
    ap.add_argument("--no-recursive", action="store_true", help="文件夹不递归")
    ap.add_argument("-v", "--version", action="version", version=f"{APP_NAME} {VERSION}")
    args = ap.parse_args(argv)

    files = collect_inputs(args.inputs, recursive=not args.no_recursive)
    if not files:
        print("没有找到可处理的文件")
        return 2

    print(f"待处理 {len(files)} 个文件，并行 {args.jobs}")
    ok = skip = fail = 0
    t0 = time.time()

    def work(p):
        return p, convert_file(p, args.out, args.bitrate, args.naming,
                               skip_existing=not args.no_skip, cover=args.cover,
                               embed_cover=args.embed_cover)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for p, (status, msg) in pool.map(work, files):
            name = os.path.basename(p)
            if status == "ok":
                ok += 1
                print(f"  ✔ {name}  {msg}")
            elif status == "skip":
                skip += 1
                print(f"  - {name}  {msg}")
            else:
                fail += 1
                print(f"  ✘ {name}  {msg}")
    print(f"\n完成：成功 {ok}，跳过 {skip}，失败 {fail}，用时 {time.time()-t0:.1f}s")
    return 0 if fail == 0 else 1


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.files = []
        self.queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.running = False

        root.title(f"{APP_NAME} {VERSION}   ·   网易云 .ncm → MP3")
        root.geometry("920x660")
        root.minsize(780, 540)
        self._build_ui()
        self.root.after(100, self._drain_queue)
        self._check_ffmpeg()

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

        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=True, **pad)
        self.tree = ttk.Treeview(mid, columns=("name", "status"), show="headings", height=12)
        self.tree.heading("name", text="文件")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=650, anchor="w")
        self.tree.column("status", width=170, anchor="center")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        opt = ttk.LabelFrame(self.root, text="输出设置")
        opt.pack(fill="x", **pad)
        row1 = ttk.Frame(opt)
        row1.pack(fill="x", padx=8, pady=6)
        ttk.Label(row1, text="输出目录：").pack(side="left")
        self.var_outdir = tk.StringVar(value="")
        ttk.Entry(row1, textvariable=self.var_outdir).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row1, text="选择…", command=self.choose_outdir).pack(side="left")
        ttk.Button(row1, text="与源同目录",
                   command=lambda: self.var_outdir.set("")).pack(side="left", padx=4)

        row2 = ttk.Frame(opt)
        row2.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(row2, text="输出格式：").pack(side="left")
        self.var_fmt = tk.StringVar(value=BITRATES[1][0])
        ttk.Combobox(row2, textvariable=self.var_fmt,
                     values=[b[0] for b in BITRATES], state="readonly",
                     width=20).pack(side="left", padx=(0, 12))
        ttk.Label(row2, text="命名：").pack(side="left")
        self.var_name = tk.StringVar(value=NAME_TPL[0][0])
        ttk.Combobox(row2, textvariable=self.var_name,
                     values=[n[0] for n in NAME_TPL], state="readonly",
                     width=16).pack(side="left", padx=(0, 12))
        ttk.Label(row2, text="并行：").pack(side="left")
        self.var_jobs = tk.IntVar(value=max(2, min(8, os.cpu_count() or 4)))
        ttk.Spinbox(row2, from_=1, to=16, textvariable=self.var_jobs,
                    width=5).pack(side="left", padx=(0, 12))
        self.var_skip = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="跳过已存在",
                        variable=self.var_skip).pack(side="left")
        self.var_cover = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="导出封面",
                        variable=self.var_cover).pack(side="left", padx=6)
        self.var_embed = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="嵌入封面",
                        variable=self.var_embed).pack(side="left")

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

    # --- 列表 ---
    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        for p, st in self.files:
            self.tree.insert("", "end", values=(os.path.basename(p), st))
        self.lbl_count.config(text=f"共 {len(self.files)} 个文件")

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择 .ncm 或音频文件",
            filetypes=[("网易云音乐", "*.ncm"),
                       ("音频文件", "*.flac *.mp3 *.m4a *.wav *.aac *.ogg"),
                       ("所有文件", "*.*")])
        self._add(list(paths))

    def add_folder(self):
        d = filedialog.askdirectory(title="选择包含 .ncm 的文件夹")
        if d:
            found = collect_inputs([d])
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
        if self.running:
            return
        sel = self.tree.selection()
        for i in sorted((self.tree.index(x) for x in sel), reverse=True):
            self.files.pop(i)
        self._refresh()

    def clear(self):
        if not self.running:
            self.files.clear()
            self._refresh()

    def choose_outdir(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.var_outdir.set(d)

    # --- 转换 ---
    def start(self):
        if self.running or not self.files:
            return
        out_dir = self.var_outdir.get().strip()
        if out_dir and not os.path.isdir(out_dir):
            messagebox.showerror("错误", "输出目录不存在。")
            return
        self.running = True
        self.stop_flag.clear()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.progress["value"] = 0
        opts = dict(out_dir=out_dir or None,
                    bitrate=dict(BITRATES)[self.var_fmt.get()],
                    naming=dict(NAME_TPL)[self.var_name.get()],
                    skip_existing=self.var_skip.get(),
                    cover=self.var_cover.get(),
                    embed_cover=self.var_embed.get(),
                    jobs=max(1, int(self.var_jobs.get())))
        threading.Thread(target=self._worker, args=(opts,), daemon=True).start()

    def stop(self):
        self.stop_flag.set()
        self.write_log("正在停止…")

    def _worker(self, opts):
        files = list(self.files)
        total = len(files)
        t0 = time.time()
        ok = skip = fail = 0
        jobs = opts.pop("jobs")

        def one(idx_path):
            idx, path = idx_path
            if self.stop_flag.is_set():
                return idx, "已取消"
            self.queue.put(("status", idx, "处理中…"))
            status, msg = convert_file(path, **opts)
            if status == "ok":
                return idx, msg
            if status == "skip":
                return idx, "已存在，跳过"
            return idx, "失败：" + msg

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for done, (idx, status) in enumerate(pool.map(one, list(enumerate(files))), 1):
                if status.startswith("完成"):
                    ok += 1
                elif status.startswith("已存在"):
                    skip += 1
                elif status.startswith("已取消"):
                    pass
                else:
                    fail += 1
                self.queue.put(("done", idx, status))
                self.queue.put(("progress", done * 100 // max(1, total)))

        self.queue.put(("finish", ok, fail, skip, time.time() - t0))

    # --- 队列 ---
    def write_log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _set_status(self, idx, st):
        kids = self.tree.get_children()
        if 0 <= idx < len(kids):
            self.tree.set(kids[idx], "status", st)

    def _drain_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "status":
                    _, idx, st = msg
                    self._set_status(idx, st)
                elif kind == "done":
                    _, idx, st = msg
                    self._set_status(idx, st)
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
    argv = sys.argv[1:]
    if "--cli" in argv:
        return cli_main([a for a in argv if a != "--cli"])
    root = tk.Tk()
    try:
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
    except Exception:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
