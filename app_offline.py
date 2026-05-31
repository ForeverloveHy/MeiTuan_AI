# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import threading
import queue
import traceback
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_ROOT = Path(os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__)))
SRC_ROOT = APP_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from sceg2.demo_runner import run_offline_project  # noqa: E402
from sceg2.longcat_client import DEFAULT_BASE_URL, DEFAULT_MODEL  # noqa: E402
from sceg2.version import CORE_VERSION  # noqa: E402


class OfflineApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"复杂指令对话检查系统｜离线状态图评估 Demo｜{CORE_VERSION}")
        self.geometry("1040x680")
        self.minsize(900, 560)
        self.result: dict | None = None
        self._ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._main_thread_id = threading.get_ident()
        self._build_ui()
        self.after(100, self._drain_ui_queue)

    def _build_ui(self) -> None:
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        title = ttk.Frame(frm)
        title.pack(fill=tk.X)
        ttk.Label(title, text="离线状态图评估", font=("Microsoft YaHei", 15, "bold")).pack(side=tk.LEFT)
        ttk.Button(title, text="打开项目目录", command=self.open_project_dir).pack(side=tk.RIGHT)

        desc = (
            "这个版本不输入复杂指令，也不执行建图。请直接选择已有 graph.json，"
            "再选择本地对话目录，系统会用同一套本地评估内核生成 JSON、HTML 和上传包。"
        )
        ttk.Label(frm, text=desc, foreground="#555", wraplength=980).pack(fill=tk.X, pady=(8, 8))

        cfg = ttk.LabelFrame(frm, text="离线输入", padding=10)
        cfg.pack(fill=tk.X)

        ttk.Label(cfg, text="离线 graph.json").grid(row=0, column=0, sticky="w")
        self.graph_path = ttk.Entry(cfg, width=80)
        self.graph_path.grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(cfg, text="选择", command=self.choose_graph).grid(row=0, column=2, sticky="w")

        ttk.Label(cfg, text="本地对话根目录").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.dialogue_root = ttk.Entry(cfg, width=80)
        self.dialogue_root.insert(0, str(APP_ROOT / "data" / "dialogues"))
        self.dialogue_root.grid(row=1, column=1, sticky="we", padx=6, pady=(6, 0))
        ttk.Button(cfg, text="选择", command=self.choose_dialogue_root).grid(row=1, column=2, sticky="w", pady=(6, 0))

        ttk.Label(cfg, text="评估范围").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.pack_choice = ttk.Combobox(cfg, state="readonly", width=20, values=["全部数据", "只跑正包", "只跑负包"])
        self.pack_choice.set("全部数据")
        self.pack_choice.grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="对话上限").grid(row=2, column=1, sticky="e", pady=(6, 0), padx=(0, 120))
        self.max_count = ttk.Entry(cfg, width=10)
        self.max_count.grid(row=2, column=1, sticky="e", padx=(0, 18), pady=(6, 0))

        ttk.Label(cfg, text="二级判断").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.llm_mode = ttk.Combobox(cfg, state="readonly", width=20, values=["关闭", "审计模式", "辅助模式"])
        self.llm_mode.set("关闭")
        self.llm_mode.grid(row=3, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="最多判断点").grid(row=3, column=1, sticky="e", pady=(6, 0), padx=(0, 120))
        self.llm_max_items = ttk.Combobox(cfg, width=10, values=["36", "100", "无限制"])
        self.llm_max_items.set("无限制")
        self.llm_max_items.grid(row=3, column=1, sticky="e", padx=(0, 18), pady=(6, 0))

        ttk.Label(cfg, text="API Key（仅二级判断需要）").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.api_key = ttk.Entry(cfg, show="*", width=52)
        self.api_key.insert(0, os.getenv("LONGCAT_API_KEY", ""))
        self.api_key.grid(row=4, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="Base URL").grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.base_url = ttk.Entry(cfg, width=52)
        self.base_url.insert(0, os.getenv("LONGCAT_BASE_URL", DEFAULT_BASE_URL))
        self.base_url.grid(row=5, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="Model").grid(row=6, column=0, sticky="w", pady=(6, 0))
        self.model = ttk.Entry(cfg, width=32)
        self.model.insert(0, os.getenv("LONGCAT_MODEL", DEFAULT_MODEL))
        self.model.grid(row=6, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="报告类型").grid(row=7, column=0, sticky="w", pady=(6, 0))
        self.report_mode = ttk.Combobox(cfg, state="readonly", width=20, values=["简版结果报告", "详细过程报告"])
        self.report_mode.set("详细过程报告")
        self.report_mode.grid(row=7, column=1, sticky="w", padx=6, pady=(6, 0))

        cfg.columnconfigure(1, weight=1)

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=10)
        self.run_btn = ttk.Button(btns, text="读取离线图并评估", command=self.run)
        self.run_btn.pack(side=tk.LEFT)
        ttk.Button(btns, text="打开 HTML 报告", command=self.open_report).pack(side=tk.LEFT, padx=8)
        ttk.Button(btns, text="打开结果目录", command=self.open_bundle_dir).pack(side=tk.LEFT)
        ttk.Button(btns, text="另存上传包", command=self.save_bundle_as).pack(side=tk.LEFT, padx=8)

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(frm, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(0, 8))
        self.status = tk.StringVar(value=f"准备就绪。本地评估内核：{CORE_VERSION}")
        ttk.Label(frm, textvariable=self.status, foreground="#444").pack(fill=tk.X)
        self.log = tk.Text(frm, height=16, wrap=tk.WORD, font=("Consolas", 9), bg="#111", fg="#e6e6e6")
        self.log.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

    def _is_ui_thread(self) -> bool:
        return threading.get_ident() == self._main_thread_id

    def _post_ui(self, kind: str, payload: object = None) -> None:
        self._ui_queue.put((kind, payload))

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "progress":
                    self._apply_progress(payload if isinstance(payload, dict) else {})
                elif kind == "done":
                    self.progress_var.set(100.0)
                    self.status.set("离线评估完成。")
                elif kind == "failed":
                    self.status.set("运行失败：" + str(payload))
                    messagebox.showerror("运行失败", str(payload))
                elif kind == "enable":
                    self.run_btn.config(state=tk.NORMAL)
        except queue.Empty:
            pass
        finally:
            self.after(100, self._drain_ui_queue)

    def _append_log(self, msg: str) -> None:
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)
        self.update_idletasks()

    def log_msg(self, msg: object) -> None:
        if self._is_ui_thread():
            self._append_log(str(msg))
        else:
            self._post_ui("log", str(msg))

    def choose_graph(self) -> None:
        p = filedialog.askopenfilename(title="选择离线 graph.json", filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if p:
            self.graph_path.delete(0, tk.END)
            self.graph_path.insert(0, p)

    def choose_dialogue_root(self) -> None:
        p = filedialog.askdirectory(title="选择对话根目录")
        if p:
            self.dialogue_root.delete(0, tk.END)
            self.dialogue_root.insert(0, p)

    def _pack_filter(self) -> str | None:
        choice = self.pack_choice.get()
        if choice.startswith("只跑正"):
            return "positive"
        if choice.startswith("只跑负"):
            return "negative"
        return None

    def _llm_mode(self) -> str:
        choice = self.llm_mode.get()
        if "辅助" in choice:
            return "assist"
        if "审计" in choice:
            return "shadow"
        return "off"

    def _llm_max_items(self) -> int | None:
        value = self.llm_max_items.get().strip()
        if value.lower() in {"无限制", "不限制", "unlimited", "all", "*", "-1"}:
            return -1
        try:
            return int(value) if value else None
        except Exception:
            return None

    def _report_mode(self) -> str:
        return "detail" if "详细" in self.report_mode.get() else "simple"

    def run(self) -> None:
        graph_path = self.graph_path.get().strip()
        if not graph_path:
            messagebox.showwarning("缺少离线图", "请先选择已有 graph.json。")
            return
        try:
            max_count = int(self.max_count.get().strip()) if self.max_count.get().strip() else None
        except Exception:
            messagebox.showwarning("参数错误", "对话上限必须是整数。")
            return
        self.progress_var.set(0.0)
        self.log.delete("1.0", tk.END)
        self.run_btn.config(state=tk.DISABLED)
        cfg = {
            "graph_path": graph_path,
            "dialogue_root": self.dialogue_root.get().strip() or str(APP_ROOT / "data" / "dialogues"),
            "max_count": max_count,
            "pack_type": self._pack_filter(),
            "llm_mode": self._llm_mode(),
            "llm_max_items": self._llm_max_items(),
            "api_key": self.api_key.get().strip(),
            "base_url": self.base_url.get().strip(),
            "model": self.model.get().strip(),
            "report_mode": self._report_mode(),
        }
        t = threading.Thread(target=self._run_worker, args=(cfg,), daemon=True)
        t.start()

    def _run_worker(self, cfg: dict) -> None:
        try:
            self.log_msg("项目目录：%s" % APP_ROOT)
            self.log_msg("离线图：%s" % cfg["graph_path"])
            self.log_msg("对话目录：%s" % cfg["dialogue_root"])
            self.log_msg("本地评估内核：%s" % CORE_VERSION)
            self.result = run_offline_project(
                graph_path=cfg["graph_path"],
                project_root=APP_ROOT,
                dialogue_root=cfg["dialogue_root"],
                max_dialogues=cfg["max_count"],
                pack_type=cfg["pack_type"],
                llm_verifier_mode=cfg["llm_mode"],
                llm_verifier_max_items=cfg["llm_max_items"],
                longcat_api_key=cfg["api_key"],
                longcat_base_url=cfg["base_url"],
                longcat_model=cfg["model"],
                report_mode=cfg["report_mode"],
                progress_callback=self._progress_callback,
            )
            self.log_msg("评估 JSON = %s" % self.result["all_reports_merged"])
            self.log_msg("HTML 报告 = %s" % self.result["html_report"])
            self.log_msg("上传压缩包 = %s" % self.result["upload_bundle"])
            self.log_msg("对话数 = %s，图来源 = %s" % (self.result["dialogue_count"], self.result["graph_source"]))
            self._post_ui("done")
        except Exception as exc:
            self.log_msg(traceback.format_exc())
            self._post_ui("failed", str(exc))
        finally:
            self._post_ui("enable")

    def _progress_callback(self, rec: dict) -> None:
        self._post_ui("progress", dict(rec))

    def _apply_progress(self, rec: dict) -> None:
        stage = rec.get("stage")
        current = float(rec.get("current") or 0)
        total = max(float(rec.get("total") or 1), 1.0)
        pct = max(0.0, min(100.0, current / total * 100.0))
        if stage == "evaluate" and rec.get("dialogue_count"):
            pct = 50.0 + 35.0 * float(rec.get("current") or 0) / max(float(rec.get("dialogue_count") or 1), 1.0)
        stage_base = {
            "load_graph": 8.0,
            "load_dialogues": 18.0,
            "filter_dialogues": 32.0,
            "evaluate": 50.0,
            "llm_verifier": 88.0,
            "summaries": 94.0,
            "bundle": 98.0,
        }
        pct = max(pct, stage_base.get(str(stage), 0.0))
        self.progress_var.set(max(self.progress_var.get(), pct))
        msg = rec.get("message") or "正在运行"
        self.status.set(str(msg))
        self.log_msg("[%s%%] %s" % (int(self.progress_var.get()), msg))

    def open_project_dir(self) -> None:
        webbrowser.open(str(APP_ROOT))

    def open_report(self) -> None:
        if self.result and self.result.get("html_report"):
            webbrowser.open(self.result["html_report"])
        else:
            messagebox.showinfo("暂无报告", "请先完成一次离线评估。")

    def open_bundle_dir(self) -> None:
        if self.result and self.result.get("upload_bundle"):
            webbrowser.open(str(Path(self.result["upload_bundle"]).parent))
        else:
            messagebox.showinfo("暂无结果", "请先完成一次离线评估。")

    def save_bundle_as(self) -> None:
        if not self.result or not self.result.get("upload_bundle"):
            messagebox.showinfo("暂无结果", "请先完成一次离线评估。")
            return
        import shutil
        src = Path(self.result["upload_bundle"])
        dst = filedialog.asksaveasfilename(title="另存上传压缩包", defaultextension=".zip", filetypes=[("ZIP", "*.zip")])
        if dst:
            shutil.copy2(src, dst)
            messagebox.showinfo("完成", "已另存：%s" % dst)


if __name__ == "__main__":
    OfflineApp().mainloop()
