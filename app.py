# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import threading
import queue
import traceback
import webbrowser
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_ROOT = Path(os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__)))
SRC_ROOT = APP_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from sceg.demo_runner import run_project  # noqa: E402
from sceg.io_utils import read_json  # noqa: E402
from sceg.longcat_client import DEFAULT_BASE_URL, DEFAULT_MODEL  # noqa: E402

DEFAULT_INSTRUCTION = """请在这里粘贴复杂客服指令。

本 demo 保留“界面输入 LongCat Key + 可选 LLM 辅助”的交互方式：
1. 在界面里输入 LongCat API Key；
2. 点击按钮后，用 LongCat 离线生成状态图、知识表和限制表；
3. 用项目最新本地评估内核读取 data/dialogues 下的最新正负包；
4. 可选择关闭、审计模式或辅助模式的大模型二级判断；
5. 输出中文 HTML 报告和 upload_bundle.zip。
"""


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("复杂指令对话检查系统｜LLM 可选 Demo + 最新评估内核")
        self.geometry("1080x780")
        self.minsize(940, 640)
        self.result = None
        self._run_started_at = None
        self._last_progress_stage = None
        self._last_elapsed_display_second = None
        self._phase_started_at: dict[str, float] = {}
        self._phase_done_seconds: dict[str, float | str] = {}
        self._main_thread_id = threading.get_ident()
        self._ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._run_active = False
        self._build_ui()
        self.after(100, self._drain_ui_queue)

    def _build_ui(self) -> None:
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(frm)
        top.pack(fill=tk.X)
        ttk.Label(top, text="复杂指令输入", font=("Microsoft YaHei", 14, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="打开项目目录", command=self.open_project_dir).pack(side=tk.RIGHT)

        self.txt = tk.Text(frm, height=18, wrap=tk.WORD, font=("Microsoft YaHei", 10))
        self.txt.pack(fill=tk.BOTH, expand=True, pady=(8, 8))
        self.txt.insert("1.0", DEFAULT_INSTRUCTION)

        cfg = ttk.LabelFrame(frm, text="配置：LongCat 只离线建图；评估使用最新本地 schema 内核与最新正负包", padding=10)
        cfg.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(cfg, text="LongCat API Key（必填）").grid(row=0, column=0, sticky="w")
        self.api_key = ttk.Entry(cfg, show="*", width=52)
        self.api_key.insert(0, os.getenv("LONGCAT_API_KEY", ""))
        self.api_key.grid(row=0, column=1, sticky="we", padx=6)

        ttk.Label(cfg, text="Base URL").grid(row=0, column=2, sticky="w")
        self.base_url = ttk.Entry(cfg, width=38)
        self.base_url.insert(0, os.getenv("LONGCAT_BASE_URL", DEFAULT_BASE_URL))
        self.base_url.grid(row=0, column=3, sticky="we", padx=6)

        ttk.Label(cfg, text="Model").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.model = ttk.Entry(cfg, width=32)
        self.model.insert(0, os.getenv("LONGCAT_MODEL", DEFAULT_MODEL))
        self.model.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="LongCat 超时").grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Label(cfg, text="不限制", foreground="#666").grid(row=1, column=3, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="建图模式").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.graph_mode = ttk.Combobox(cfg, state="readonly", width=28, values=["快速建图（只补硬缺口）", "稳健建图（质量缺口也补）", "只建一次（跳过补图）"])
        self.graph_mode.set("快速建图（只补硬缺口）")
        self.graph_mode.grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="评估数据包").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.pack_choice = ttk.Combobox(cfg, state="readonly", width=22, values=["全部数据", "只跑正包", "只跑负包"])
        self.pack_choice.set("全部数据")
        self.pack_choice.grid(row=3, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="评估对话上限").grid(row=3, column=2, sticky="w", pady=(6, 0))
        self.max_count = ttk.Entry(cfg, width=10)
        self.max_count.insert(0, "")
        self.max_count.grid(row=3, column=3, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="大模型二级判断").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.llm_mode = ttk.Combobox(cfg, state="readonly", width=22, values=["关闭", "审计模式", "辅助模式"])
        self.llm_mode.set("辅助模式")
        self.llm_mode.grid(row=4, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="大模型最多判断点").grid(row=4, column=2, sticky="w", pady=(6, 0))
        self.llm_max_items = ttk.Combobox(cfg, width=10, values=["36", "100", "无限制"])
        self.llm_max_items.set("无限制")
        self.llm_max_items.grid(row=4, column=3, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="报告类型").grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.report_mode = ttk.Combobox(cfg, state="readonly", width=22, values=["简版结果报告", "详细过程报告"])
        self.report_mode.set("详细过程报告")
        self.report_mode.grid(row=5, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="本地对话根目录").grid(row=6, column=0, sticky="w", pady=(6, 0))
        self.dialogue_root = ttk.Entry(cfg, width=70)
        self.dialogue_root.insert(0, str(APP_ROOT / "data" / "dialogues"))
        self.dialogue_root.grid(row=6, column=1, columnspan=2, sticky="we", padx=6, pady=(6, 0))
        ttk.Button(cfg, text="选择", command=self.choose_dialogue_root).grid(row=6, column=3, sticky="w", padx=6, pady=(6, 0))

        cfg.columnconfigure(1, weight=1)
        cfg.columnconfigure(3, weight=1)

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=8)
        self.run_btn = ttk.Button(btns, text="一键生成状态图并评估最新正负包", command=self.run)
        self.run_btn.pack(side=tk.LEFT)
        ttk.Button(btns, text="打开中文 HTML 报告", command=self.open_report).pack(side=tk.LEFT, padx=8)
        ttk.Button(btns, text="打开结果压缩包位置", command=self.open_bundle_dir).pack(side=tk.LEFT)
        ttk.Button(btns, text="另存上传压缩包", command=self.save_bundle_as).pack(side=tk.LEFT, padx=8)

        prog = ttk.Frame(frm)
        prog.pack(fill=tk.X, pady=(0, 6))
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(prog, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.elapsed_var = tk.StringVar(value="进度：0% ｜ 总用时：0 秒 ｜ 一次建图：未开始 ｜ 二次补图：未开始")
        ttk.Label(prog, textvariable=self.elapsed_var, width=64, anchor="e").pack(side=tk.RIGHT, padx=(8, 0))

        self.status = tk.StringVar(value="准备就绪。请粘贴复杂指令并填写 LongCat Key。")
        ttk.Label(frm, textvariable=self.status, foreground="#444").pack(fill=tk.X, pady=(4, 4))
        self.log = tk.Text(frm, height=10, wrap=tk.WORD, font=("Consolas", 9), bg="#111", fg="#e6e6e6")
        self.log.pack(fill=tk.BOTH, expand=False)

    def _is_ui_thread(self) -> bool:
        return threading.get_ident() == self._main_thread_id

    def _post_ui(self, kind: str, payload: object = None) -> None:
        self._ui_queue.put((kind, payload))

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    self._apply_progress(payload if isinstance(payload, dict) else {})
                elif kind == "complete":
                    self._mark_complete()
                elif kind == "failed":
                    self._mark_failed(str(payload))
                elif kind == "enable_button":
                    self.run_btn.config(state=tk.NORMAL)
        except queue.Empty:
            pass
        finally:
            self.after(100, self._drain_ui_queue)

    def _append_log(self, msg: object) -> None:
        self.log.insert(tk.END, str(msg) + "\n")
        self.log.see(tk.END)
        self.update_idletasks()

    def log_msg(self, msg: object) -> None:
        if not self._is_ui_thread():
            self._post_ui("log", msg)
            return
        self._append_log(msg)

    def run(self) -> None:
        instruction = self.txt.get("1.0", tk.END).strip()
        if not instruction or instruction == DEFAULT_INSTRUCTION.strip():
            messagebox.showwarning("缺少输入", "请先粘贴真实复杂指令。")
            return
        api_key = self.api_key.get().strip() or os.getenv("LONGCAT_API_KEY", "")
        if not api_key:
            messagebox.showwarning("缺少 LongCat Key", "请先填写 LongCat API Key，或设置 LONGCAT_API_KEY。")
            return
        self.run_btn.config(state=tk.DISABLED)
        self._run_started_at = time.perf_counter()
        self._run_active = True
        self._last_progress_stage = None
        self._last_elapsed_display_second = None
        self._phase_started_at = {}
        self._phase_done_seconds = {}
        self.progress_var.set(0.0)
        self.elapsed_var.set("进度：0% ｜ 总用时：0 秒 ｜ 一次建图：未开始 ｜ 二次补图：未开始")
        self.status.set("正在运行：LongCat 离线建图 → 最新本地评估 → 中文报告...")
        self.log.delete("1.0", tk.END)
        try:
            max_count = None
            mc = self.max_count.get().strip()
            if mc:
                max_count = int(mc)
        except Exception as exc:
            self._run_active = False
            self.run_btn.config(state=tk.NORMAL)
            messagebox.showwarning("参数错误", "评估对话上限必须是整数：%s" % exc)
            return
        run_cfg = {
            "max_count": max_count,
            "dialogue_root": self._resolved_dialogue_root(),
            "api_key": self.api_key.get().strip() or os.getenv("LONGCAT_API_KEY", ""),
            "base_url": self.base_url.get().strip(),
            "model": self.model.get().strip(),
            "pack_label": self.pack_choice.get(),
            "pack_type": self._pack_filter(),
            "graph_mode_label": self.graph_mode.get() if hasattr(self, "graph_mode") else "快速建图（只补硬缺口）",
            "llm_label": self.llm_mode.get(),
            "llm_mode": self._llm_mode(),
            "llm_max_items": self._llm_max_items(),
            "report_mode": self._report_mode(),
            "repair_mode": self._repair_mode(),
            "use_graph_cache": True,
        }
        self._tick_elapsed()
        t = threading.Thread(target=self._run_worker, args=(instruction, run_cfg))
        t.daemon = True
        t.start()

    def _run_worker(self, instruction: str, run_cfg: dict) -> None:
        try:
            dialogue_root = run_cfg.get("dialogue_root")
            self.log_msg("项目目录：%s" % APP_ROOT)
            self.log_msg("第 1 步：调用 LongCat 离线生成状态图、知识表和限制表")
            self.log_msg("LongCat Base URL：%s" % run_cfg.get("base_url"))
            self.log_msg("LongCat Model：%s" % run_cfg.get("model"))
            self.log_msg("LongCat 超时：不限制")
            self.log_msg("对话目录：%s" % dialogue_root)
            self.log_msg("数据包：%s" % run_cfg.get("pack_label"))
            self.log_msg("建图模式：%s（快速=只在硬缺口时二次补图；稳健=质量警告也补；只建一次=跳过补图）" % run_cfg.get("graph_mode_label"))
            self.log_msg("大模型二级判断：%s（关闭=纯本地；审计=只记录；辅助=可把待仲裁负包改为仲裁通过）" % run_cfg.get("llm_label"))
            self.result = run_project(
                instruction=instruction,
                project_root=APP_ROOT,
                longcat_api_key=str(run_cfg.get("api_key") or ""),
                longcat_base_url=str(run_cfg.get("base_url") or ""),
                longcat_model=str(run_cfg.get("model") or ""),
                longcat_timeout=None,
                dialogue_root=dialogue_root or None,
                max_dialogues=run_cfg.get("max_count"),
                pack_type=run_cfg.get("pack_type"),
                llm_verifier_mode=str(run_cfg.get("llm_mode") or "off"),
                llm_verifier_max_items=run_cfg.get("llm_max_items"),
                report_mode=str(run_cfg.get("report_mode") or "detail"),
                progress_callback=self._progress_callback,
                repair_mode=str(run_cfg.get("repair_mode") or "blocking"),
                use_graph_cache=bool(run_cfg.get("use_graph_cache", True)),
            )
            self.log_msg("第 2 步：已读取最新对话目录 %s" % self.result["dialogue_root"])
            self.log_msg("第 3 步：评估 JSON = %s" % self.result["all_reports_merged"])
            self.log_msg("第 4 步：中文 HTML 报告 = %s" % self.result["html_report"])
            self.log_msg("上传压缩包 = %s" % self.result["upload_bundle"])
            self.log_msg("Token 用量 JSON = %s" % self.result.get("run_token_usage"))
            self.log_msg("分段计时 JSON = %s" % self.result.get("run_timing_summary"))
            self.log_msg("大模型二级判断摘要 = %s" % self.result.get("llm_verifier_summary"))
            try:
                token_usage = read_json(self.result.get("run_token_usage")) if self.result.get("run_token_usage") else {}
                total_tokens = ((token_usage.get("total") or {}).get("total_tokens") or 0)
                self.log_msg("Token 用量：总计 %s" % total_tokens)
            except Exception:
                pass
            self.log_msg("对话数 = %s，状态图来源 = %s，建图模式 = %s，缓存命中 = %s" % (self.result["dialogue_count"], self.result["graph_source"], self.result.get("repair_mode"), self.result.get("longcat_cache_hit")))
            self._post_ui("complete", None)
        except Exception as exc:
            self.log_msg(traceback.format_exc())
            self._post_ui("failed", str(exc))
        finally:
            self._post_ui("enable_button", None)

    def _repair_mode(self) -> str:
        choice = self.graph_mode.get() if hasattr(self, "graph_mode") else "快速建图（只补硬缺口）"
        if "稳健" in choice:
            return "quality"
        if "只建一次" in choice or "跳过" in choice:
            return "off"
        return "blocking"

    def _report_mode(self) -> str:
        choice = self.report_mode.get() if hasattr(self, "report_mode") else "详细过程报告"
        return "detail" if "详细" in choice else "simple"

    def _pack_filter(self) -> str | None:
        choice = self.pack_choice.get() if hasattr(self, "pack_choice") else "全部数据"
        if choice.startswith("只跑正"):
            return "positive"
        if choice.startswith("只跑负"):
            return "negative"
        return None

    def _llm_mode(self) -> str:
        choice = self.llm_mode.get() if hasattr(self, "llm_mode") else "辅助模式"
        if "辅助" in choice:
            return "assist"
        if "审计" in choice:
            return "shadow"
        return "off"

    def _llm_max_items(self) -> int | None:
        try:
            value = self.llm_max_items.get().strip() if hasattr(self, "llm_max_items") else "无限制"
            if value.lower() in {"无限制", "不限制", "unlimited", "all", "*", "-1"}:
                return -1
            return int(value) if value else None
        except Exception:
            return None

    def _resolved_dialogue_root(self) -> str:
        base = self.dialogue_root.get().strip() or str(APP_ROOT / "data" / "dialogues")
        choice = self.pack_choice.get() if hasattr(self, "pack_choice") else "全部数据"
        if choice.startswith("只跑正") and os.path.basename(base) != "positive_pack":
            cand = os.path.join(base, "positive_pack")
            return cand if os.path.exists(cand) else base
        if choice.startswith("只跑负") and os.path.basename(base) != "negative_pack":
            cand = os.path.join(base, "negative_pack")
            return cand if os.path.exists(cand) else base
        return base

    def _progress_callback(self, rec: dict) -> None:
        self._post_ui("progress", dict(rec))

    def _apply_progress(self, rec: dict) -> None:
        stage = rec.get("stage")
        self._apply_phase_event(rec)
        current = rec.get("current") or 0
        total = rec.get("total") or 1
        if stage == "evaluate" and rec.get("dialogue_count"):
            pct = 25.0 + 45.0 * float(current) / max(float(rec.get("dialogue_count") or total or 1), 1.0)
        else:
            stage_pct = {
                "build_graph": 8.0,
                "longcat_build_graph": 14.0,
                "longcat_repair_graph": 20.0,
                "load_dialogues": 22.0,
                "filter_dialogues": 24.0,
                "evaluate": 30.0,
                "llm_verifier": 72.0,
                "summaries": 80.0,
                "html_reports": 90.0,
                "bundle": 96.0,
            }
            pct = stage_pct.get(stage, self.progress_var.get())
        pct = max(0.0, min(100.0, float(pct)))
        if pct >= self.progress_var.get():
            self.progress_var.set(pct)
        msg = rec.get("message") or "正在评估"
        self.status.set(msg)
        loggable_stages = {"build_graph", "longcat_build_graph", "longcat_repair_graph", "load_dialogues", "filter_dialogues", "llm_verifier", "summaries", "html_reports", "bundle"}
        event_key = "%s:%s" % (stage, rec.get("event") or "")
        if event_key != self._last_progress_stage or stage in loggable_stages:
            self.log_msg("[%s%%] %s" % (int(self.progress_var.get()), msg))
            self._last_progress_stage = event_key
        self._refresh_elapsed_label()

    def _apply_phase_event(self, rec: dict) -> None:
        phase = rec.get("phase")
        event = rec.get("event")
        if phase not in {"longcat_build_graph", "longcat_repair_graph"}:
            return
        now = time.perf_counter()
        if event == "start":
            self._phase_started_at[phase] = now
            self._phase_done_seconds.pop(phase, None)
        elif event == "done":
            elapsed = rec.get("elapsed_seconds")
            try:
                elapsed_value = float(elapsed)
            except Exception:
                start = self._phase_started_at.get(phase, now)
                elapsed_value = max(0.0, now - start)
            self._phase_done_seconds[phase] = elapsed_value
            self._phase_started_at.pop(phase, None)
        elif event == "skipped":
            self._phase_done_seconds[phase] = "未触发"
            self._phase_started_at.pop(phase, None)

    def _tick_elapsed(self) -> None:
        if not self._run_active:
            return
        self._refresh_elapsed_label()
        if self._run_active:
            self.after(1000, self._tick_elapsed)

    def _format_phase_elapsed(self, phase: str) -> str:
        done = self._phase_done_seconds.get(phase)
        if isinstance(done, str):
            return done
        if isinstance(done, (int, float)):
            return "%d 秒" % int(round(float(done)))
        start = self._phase_started_at.get(phase)
        if start is not None:
            return "%d 秒" % int(time.perf_counter() - start)
        return "未开始"

    def _refresh_elapsed_label(self, force_done: bool = False) -> None:
        elapsed = 0.0 if self._run_started_at is None else time.perf_counter() - self._run_started_at
        elapsed_second = int(elapsed)
        phase_second_key = (
            elapsed_second,
            self._format_phase_elapsed("longcat_build_graph"),
            self._format_phase_elapsed("longcat_repair_graph"),
        )
        if not force_done and self._last_elapsed_display_second == phase_second_key:
            return
        self._last_elapsed_display_second = phase_second_key
        pct = int(round(self.progress_var.get()))
        prefix = "完成" if force_done else "进度"
        self.elapsed_var.set(
            "%s：%s%% ｜ 总用时：%d 秒 ｜ 一次建图：%s ｜ 二次补图：%s"
            % (prefix, pct, elapsed_second, self._format_phase_elapsed("longcat_build_graph"), self._format_phase_elapsed("longcat_repair_graph"))
        )
        self.update_idletasks()

    def _mark_complete(self) -> None:
        self._run_active = False
        self.progress_var.set(100.0)
        self._refresh_elapsed_label(force_done=True)
        self.status.set("完成。已生成中文 HTML 报告和 upload_bundle.zip。")
        try:
            if self.result and self.result.get("html_report"):
                webbrowser.open(self.result["html_report"])
        except Exception:
            pass

    def _mark_failed(self, message: str) -> None:
        self._run_active = False
        self._refresh_elapsed_label(force_done=True)
        self.status.set("运行失败：%s" % message)
        messagebox.showerror("运行失败", message)

    def open_report(self) -> None:
        if not self.result:
            latest = APP_ROOT / "runs" / "latest_run.json"
            if latest.exists():
                self.result = read_json(latest)
        if self.result and os.path.exists(self.result.get("html_report", "")):
            webbrowser.open(self.result["html_report"])
        else:
            messagebox.showinfo("暂无报告", "请先运行一次评估。")

    def open_bundle_dir(self) -> None:
        if not self.result:
            latest = APP_ROOT / "runs" / "latest_run.json"
            if latest.exists():
                self.result = read_json(latest)
        if self.result and os.path.exists(self.result.get("upload_bundle", "")):
            path = os.path.dirname(self.result["upload_bundle"])
            os.startfile(path) if os.name == "nt" else webbrowser.open(path)
        else:
            messagebox.showinfo("暂无压缩包", "请先运行一次评估。")

    def save_bundle_as(self) -> None:
        if not self.result or not os.path.exists(self.result.get("upload_bundle", "")):
            messagebox.showinfo("暂无压缩包", "请先运行一次评估。")
            return
        dst = filedialog.asksaveasfilename(defaultextension=".zip", filetypes=[("Zip", "*.zip")], initialfile="sceg_upload_bundle.zip")
        if dst:
            import shutil

            shutil.copy2(self.result["upload_bundle"], dst)
            messagebox.showinfo("已保存", dst)

    def choose_dialogue_root(self) -> None:
        d = filedialog.askdirectory(initialdir=self.dialogue_root.get().strip() or str(APP_ROOT))
        if d:
            self.dialogue_root.delete(0, tk.END)
            self.dialogue_root.insert(0, d)

    def open_project_dir(self) -> None:
        os.startfile(APP_ROOT) if os.name == "nt" else webbrowser.open(str(APP_ROOT))


if __name__ == "__main__":
    (APP_ROOT / "data" / "dialogues" / "positive_pack").mkdir(parents=True, exist_ok=True)
    (APP_ROOT / "data" / "dialogues" / "negative_pack").mkdir(parents=True, exist_ok=True)
    App().mainloop()
