# -*- coding: utf-8 -*-
"""
app_graph.py
只用于 LLM 离线建图：输入复杂客服指令，输出 SCEG schema JSON。

运行方式：
1. 图形界面：python app_graph.py
2. 命令行：python app_graph.py --instruction instruction.txt --api-key YOUR_KEY --out runs/graphs_llm/my_graph.json

说明：
- 本脚本只调用 LLM 执行“一图两表 + Atom Registry + elements”建图，不读取正负包，不做 dialogue 评估。
- 输出包含 graph_core、knowledge_table、hard_constraint_table、soft_constraint_table、Atom Registry 锚点与 elements；合并 constraint_table 仅作为运行时兼容视图，不是权威结构。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_ROOT = Path(os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__)))
SRC_ROOT = APP_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from sceg.demo_runner import build_graph_with_llm  # noqa: E402
from sceg.io_utils import write_json  # noqa: E402
from sceg.llm_client import DEFAULT_BASE_URL, DEFAULT_MODEL  # noqa: E402

DEFAULT_INSTRUCTION = """请在这里粘贴复杂客服指令。

本工具只负责建图：
1. Pass 1 只生成状态主图 nodes/atoms/edges/relation_groups；
2. Pass 2 只生成知识表 knowledge_table；
3. Pass 3 只生成 hard_constraint_table 与 soft_constraint_table；
4. 本地生成 Atom Registry，并要求 LLM 只按 element_anchor_id 补 elements；
5. Pass 5 只扩充二级表达池 secondary_elements / secondary_pools；
6. 不读取正负包，不跑评估，不使用负包标签。
"""


def _slug(value: object, fallback: str = "graph") -> str:
    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(value or "")).strip("_-")
    return s[:80] or fallback


def _now_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _refine_mode_from_text(value: str) -> str:
    # 历史 UI 曾允许快速/稳健/跳过分支。当前机制中一级 elements 是必跑阶段，
    # 该函数只保留接口兼容，不再允许用户输入改变建图链路。
    return "required"


def _write_outputs(graph_data: dict, out_path: str | Path | None = None) -> dict[str, str]:
    graph_id = graph_data.get("graph_id") or graph_data.get("name") or "llm_graph"
    if out_path:
        graph_path = Path(out_path)
        if graph_path.suffix.lower() != ".json":
            graph_path.mkdir(parents=True, exist_ok=True)
            graph_path = graph_path / f"{_slug(graph_id)}.json"
    else:
        graph_dir = APP_ROOT / "runs" / "graphs_llm"
        graph_dir.mkdir(parents=True, exist_ok=True)
        graph_path = graph_dir / f"{_slug(graph_id)}.json"

    graph_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(graph_path, graph_data)

    latest_dir = APP_ROOT / "runs" / "graph_only_latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    latest_graph_path = latest_dir / "graph.json"
    write_json(latest_graph_path, graph_data)

    meta_path = latest_dir / "graph_build_summary.json"
    metadata = graph_data.get("metadata") or {}
    usage = metadata.get("llm_token_usage") or {}
    summary = {
        "graph_id": graph_data.get("graph_id"),
        "name": graph_data.get("name"),
        "nodes": len(graph_data.get("nodes") or []),
        "edges": len(graph_data.get("edges") or []),
        "relation_groups": len(graph_data.get("relation_groups") or []),
        "knowledge_table": len(graph_data.get("knowledge_table") or []),
        "hard_constraint_table": len(graph_data.get("hard_constraint_table") or []),
        "soft_constraint_table": len(graph_data.get("soft_constraint_table") or []),
        "terminal_policies": len(graph_data.get("terminal_policies") or []),
        "llm_model": metadata.get("llm_model"),
        "llm_cache_hit": metadata.get("llm_cache_hit"),
        "llm_phase_timing_seconds": metadata.get("llm_phase_timing_seconds"),
        "token_usage_total": usage.get("total"),
        "graph_path": str(graph_path),
        "latest_graph_path": str(latest_graph_path),
    }
    write_json(meta_path, summary)
    return {"graph_path": str(graph_path), "latest_graph_path": str(latest_graph_path), "summary_path": str(meta_path)}


def build_graph_only(
    instruction: str,
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
    refine_mode: str = "required",
    out_path: str | Path | None = None,
    use_cache: bool = True,
    progress_callback=None,
) -> tuple[dict, dict[str, str]]:
    graph_data, _usage = build_graph_with_llm(
        instruction=instruction,
        project_root=APP_ROOT,
        api_key=api_key or "",
        base_url=base_url or DEFAULT_BASE_URL,
        model=model or DEFAULT_MODEL,
        timeout=None,
        binding_hints=None,
        progress_callback=progress_callback,
        refine_mode="required",
        use_cache=use_cache,
    )
    paths = _write_outputs(graph_data, out_path)
    return graph_data, paths


class GraphApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SCEG LLM 建图工具｜只生成图和表")
        self.geometry("1060x760")
        self.minsize(920, 620)
        self.result_paths: dict[str, str] | None = None
        self._main_thread_id = threading.get_ident()
        self._ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._started_at: float | None = None
        self._last_elapsed_second: int | None = None
        self._build_ui()
        self.after(100, self._drain_ui_queue)
        self.after(500, self._tick_elapsed)

    def _build_ui(self) -> None:
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(frm)
        top.pack(fill=tk.X)
        ttk.Label(top, text="复杂客服指令输入", font=("Microsoft YaHei", 14, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="打开项目目录", command=self.open_project_dir).pack(side=tk.RIGHT)

        self.txt = tk.Text(frm, height=18, wrap=tk.WORD, font=("Microsoft YaHei", 10))
        self.txt.pack(fill=tk.BOTH, expand=True, pady=(8, 8))
        self.txt.insert("1.0", DEFAULT_INSTRUCTION)

        cfg = ttk.LabelFrame(frm, text="LLM 建图配置｜不读取数据集，不跑评估", padding=10)
        cfg.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(cfg, text="LLM API Key（必填）").grid(row=0, column=0, sticky="w")
        self.api_key = ttk.Entry(cfg, show="*", width=52)
        self.api_key.insert(0, os.getenv("LLM_API_KEY", ""))
        self.api_key.grid(row=0, column=1, sticky="we", padx=6)

        ttk.Label(cfg, text="Base URL").grid(row=0, column=2, sticky="w")
        self.base_url = ttk.Entry(cfg, width=38)
        self.base_url.insert(0, os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL))
        self.base_url.grid(row=0, column=3, sticky="we", padx=6)

        ttk.Label(cfg, text="Model").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.model = ttk.Entry(cfg, width=32)
        self.model.insert(0, os.getenv("LLM_MODEL", DEFAULT_MODEL))
        self.model.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="建图模式").grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.graph_mode = tk.StringVar(value="一图两表 + atom registry 元素生成")
        ttk.Label(cfg, textvariable=self.graph_mode, foreground="#444").grid(row=1, column=3, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(cfg, text="输出路径").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.out_path = ttk.Entry(cfg, width=80)
        self.out_path.insert(0, str(APP_ROOT / "runs" / "graphs_llm" / ("graph_only_" + _now_id() + ".json")))
        self.out_path.grid(row=2, column=1, columnspan=2, sticky="we", padx=6, pady=(6, 0))
        ttk.Button(cfg, text="选择", command=self.choose_out_path).grid(row=2, column=3, sticky="w", padx=6, pady=(6, 0))

        self.use_cache = tk.BooleanVar(value=True)
        ttk.Checkbutton(cfg, text="启用建图缓存（相同指令与提示词命中时不重复调用 LLM）", variable=self.use_cache).grid(row=3, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))

        cfg.columnconfigure(1, weight=1)
        cfg.columnconfigure(3, weight=1)

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=8)
        self.run_btn = ttk.Button(btns, text="只生成状态图 JSON", command=self.run)
        self.run_btn.pack(side=tk.LEFT)
        ttk.Button(btns, text="打开生成目录", command=self.open_output_dir).pack(side=tk.LEFT, padx=8)
        ttk.Button(btns, text="打开最新 graph.json", command=self.open_latest_graph).pack(side=tk.LEFT)

        prog = ttk.Frame(frm)
        prog.pack(fill=tk.X, pady=(0, 6))
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(prog, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.elapsed_var = tk.StringVar(value="进度：0% ｜ 用时：0 秒")
        ttk.Label(prog, textvariable=self.elapsed_var, width=36, anchor="e").pack(side=tk.RIGHT, padx=(8, 0))

        self.status = tk.StringVar(value="准备就绪。粘贴复杂指令并填写 LLM Key 后开始建图。")
        ttk.Label(frm, textvariable=self.status, foreground="#444").pack(fill=tk.X, pady=(4, 4))
        self.log = tk.Text(frm, height=11, wrap=tk.WORD, font=("Consolas", 9), bg="#111", fg="#e6e6e6")
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
                    self._append_log(str(payload))
                elif kind == "progress":
                    self._apply_progress(payload if isinstance(payload, dict) else {})
                elif kind == "done":
                    self.progress_var.set(100.0)
                    self.status.set("建图完成。")
                elif kind == "failed":
                    self.status.set("建图失败：" + str(payload))
                    messagebox.showerror("建图失败", str(payload))
                elif kind == "enable":
                    self.run_btn.config(state=tk.NORMAL)
        except queue.Empty:
            pass
        finally:
            self.after(100, self._drain_ui_queue)

    def _tick_elapsed(self) -> None:
        if self._started_at is not None:
            elapsed = int(time.perf_counter() - self._started_at)
            if elapsed != self._last_elapsed_second:
                self._last_elapsed_second = elapsed
                self.elapsed_var.set(f"进度：{self.progress_var.get():.0f}% ｜ 用时：{elapsed} 秒")
        self.after(500, self._tick_elapsed)

    def _append_log(self, msg: str) -> None:
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)
        self.update_idletasks()

    def log_msg(self, msg: object) -> None:
        if self._is_ui_thread():
            self._append_log(str(msg))
        else:
            self._post_ui("log", str(msg))

    def _apply_progress(self, rec: dict) -> None:
        stage = rec.get("stage") or rec.get("phase") or "build_graph"
        event = rec.get("event") or ""
        msg = rec.get("message") or ""
        if stage in {"llm_core_graph", "llm_build_graph"}:
            val = 10 if event == "start" else 28
        elif stage == "llm_knowledge_table":
            val = 32 if event == "start" else 45
        elif stage == "llm_constraint_tables":
            val = 48 if event == "start" else 60
        elif stage in {"llm_atom_element_refinement", "llm_element_refinement"}:
            val = 64 if event == "start" else 78
        elif stage == "llm_element_expansion":
            val = 82 if event == "start" else 92
        else:
            val = min(95, self.progress_var.get() + 5)
        self.progress_var.set(float(val))
        elapsed = int(time.perf_counter() - self._started_at) if self._started_at else 0
        self.elapsed_var.set(f"进度：{self.progress_var.get():.0f}% ｜ 用时：{elapsed} 秒")
        if msg:
            self.status.set(str(msg))
            self.log_msg(msg)

    def choose_out_path(self) -> None:
        p = filedialog.asksaveasfilename(title="保存 graph.json", defaultextension=".json", filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if p:
            self.out_path.delete(0, tk.END)
            self.out_path.insert(0, p)

    def run(self) -> None:
        instruction = self.txt.get("1.0", tk.END).strip()
        api_key = self.api_key.get().strip()
        if not instruction or instruction == DEFAULT_INSTRUCTION.strip():
            messagebox.showwarning("缺少复杂指令", "请先粘贴真实复杂客服指令。")
            return
        if not api_key:
            messagebox.showwarning("缺少 API Key", "请填写 LLM API Key，或设置环境变量 LLM_API_KEY。")
            return
        self.progress_var.set(0.0)
        self.log.delete("1.0", tk.END)
        self.result_paths = None
        self._started_at = time.perf_counter()
        self._last_elapsed_second = None
        self.run_btn.config(state=tk.DISABLED)
        cfg = {
            "instruction": instruction,
            "api_key": api_key,
            "base_url": self.base_url.get().strip() or DEFAULT_BASE_URL,
            "model": self.model.get().strip() or DEFAULT_MODEL,
            "refine_mode": _refine_mode_from_text(self.graph_mode.get()),
            "out_path": self.out_path.get().strip(),
            "use_cache": bool(self.use_cache.get()),
        }
        threading.Thread(target=self._run_worker, args=(cfg,), daemon=True).start()

    def _run_worker(self, cfg: dict) -> None:
        try:
            self.log_msg("开始 LLM 建图。模式 = 一图两表 + atom registry 元素生成")
            graph_data, paths = build_graph_only(
                instruction=cfg["instruction"],
                api_key=cfg["api_key"],
                base_url=cfg.get("base_url"),
                model=cfg.get("model"),
                refine_mode="required",
                out_path=cfg.get("out_path"),
                use_cache=bool(cfg.get("use_cache", True)),
                progress_callback=lambda rec: self._post_ui("progress", rec),
            )
            self.result_paths = paths
            meta = graph_data.get("metadata") or {}
            usage = meta.get("llm_token_usage") or {}
            total = (usage.get("total") or {}).get("total_tokens")
            self.log_msg("建图完成：%s" % paths.get("graph_path"))
            self.log_msg("最新副本：%s" % paths.get("latest_graph_path"))
            self.log_msg("摘要文件：%s" % paths.get("summary_path"))
            self.log_msg("节点=%s，关系=%s，知识=%s，硬限制=%s，软限制=%s，终止策略=%s" % (
                len(graph_data.get("nodes") or []),
                len(graph_data.get("edges") or []),
                len(graph_data.get("knowledge_table") or []),
                len(graph_data.get("hard_constraint_table") or []),
                len(graph_data.get("soft_constraint_table") or []),
                len(graph_data.get("terminal_policies") or []),
            ))
            if total is not None:
                self.log_msg("Token 用量：%s" % total)
            self._post_ui("done", None)
        except Exception as exc:
            self.log_msg(traceback.format_exc())
            self._post_ui("failed", str(exc))
        finally:
            self._started_at = None
            self._post_ui("enable", None)

    def open_project_dir(self) -> None:
        webbrowser.open(str(APP_ROOT))

    def open_output_dir(self) -> None:
        if self.result_paths and self.result_paths.get("graph_path"):
            webbrowser.open(str(Path(self.result_paths["graph_path"]).parent))
        else:
            webbrowser.open(str(APP_ROOT / "runs" / "graphs_llm"))

    def open_latest_graph(self) -> None:
        p = None
        if self.result_paths:
            p = self.result_paths.get("latest_graph_path")
        if not p:
            p = str(APP_ROOT / "runs" / "graph_only_latest" / "graph.json")
        if Path(p).exists():
            webbrowser.open(str(Path(p).resolve()))
        else:
            messagebox.showinfo("暂无文件", "还没有生成 latest graph.json。")


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只调用 LLM 生成 SCEG schema graph JSON，不做评估。")
    parser.add_argument("--instruction", help="复杂客服指令 txt/md 文件路径。")
    parser.add_argument("--instruction-text", help="直接传入复杂客服指令文本。")
    parser.add_argument("--api-key", default=os.getenv("LLM_API_KEY", ""), help="LLM API Key；也可用环境变量 LLM_API_KEY。")
    parser.add_argument("--base-url", default=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL), help="LLM Base URL。")
    parser.add_argument("--model", default=os.getenv("LLM_MODEL", DEFAULT_MODEL), help="LLM 模型名，默认读取 LLM_MODEL。")
    parser.add_argument("--mode", default="required", choices=["required"], help="保留兼容参数；第二阶段element细化必跑，不能跳过。")
    parser.add_argument("--out", default="", help="输出 graph.json 路径；不填则写入 runs/graphs_llm。")
    parser.add_argument("--no-cache", action="store_true", help="禁用建图缓存。")
    parser.add_argument("--gui", action="store_true", help="启动图形界面。")
    args = parser.parse_args(argv)

    if args.gui or (not args.instruction and not args.instruction_text):
        app = GraphApp()
        app.mainloop()
        return 0

    if not args.api_key:
        raise SystemExit("缺少 LLM API Key。请使用 --api-key 或设置 LLM_API_KEY。")

    if args.instruction_text:
        instruction = args.instruction_text.strip()
    else:
        instruction = Path(args.instruction).read_text(encoding="utf-8").strip()
    if not instruction:
        raise SystemExit("复杂指令为空。")

    def progress(rec: dict) -> None:
        msg = rec.get("message") or rec
        print(msg, flush=True)

    graph_data, paths = build_graph_only(
        instruction=instruction,
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        refine_mode="required",
        out_path=args.out or None,
        use_cache=not args.no_cache,
        progress_callback=progress,
    )
    print(json.dumps({
        "ok": True,
        "graph_path": paths.get("graph_path"),
        "latest_graph_path": paths.get("latest_graph_path"),
        "summary_path": paths.get("summary_path"),
        "graph_id": graph_data.get("graph_id"),
        "nodes": len(graph_data.get("nodes") or []),
        "knowledge_table": len(graph_data.get("knowledge_table") or []),
        "hard_constraint_table": len(graph_data.get("hard_constraint_table") or []),
        "soft_constraint_table": len(graph_data.get("soft_constraint_table") or []),
        "terminal_policies": len(graph_data.get("terminal_policies") or []),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
