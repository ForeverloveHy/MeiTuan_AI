from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _load(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(data, dict):
        data = data.get('records') or data.get('reports') or data.get('cases') or []
    return list(data)


def _target(e: dict[str, Any]) -> str:
    return str(e.get('requirement_id') or e.get('knowledge_id') or e.get('constraint_id') or e.get('node_id') or e.get('target_id') or '')


def _family(e: dict[str, Any]) -> str:
    return str(e.get('error_family') or e.get('type') or 'unknown')


def summarize(records: list[dict[str, Any]], title: str) -> str:
    neg = [r for r in records if str(r.get('sample_type')).lower() == 'negative']
    lines = [f'# {title}', '', f'- 负包样本数：{len(neg)}']
    result_counts = Counter((r.get('acceptance') or {}).get('result') for r in neg)
    lines.append(f'- 验收结果：{dict(result_counts)}')
    missing = []
    unexpected = []
    for r in neg:
        acc = r.get('acceptance') or {}
        for e in acc.get('missing_expected') or []:
            missing.append((r.get('dialogue_id'), e))
        for e in acc.get('unexpected_bad_events') or []:
            unexpected.append((r.get('dialogue_id'), e))
    lines += ['', '## 未命中预设错误', '', f'- 未命中条数：{len(missing)}', f'- 涉及样本：{len({x[0] for x in missing})}']
    lines.append(f'- 按错误族：{dict(Counter(_family(e) for _id, e in missing))}')
    lines += ['', '| 样本 | 错误族 | 目标 | 预设错误原话/说明 |', '|---|---|---|---|']
    for did, e in missing[:120]:
        desc = str(e.get('wrong_statement') or e.get('evidence_span') or e.get('description') or '').replace('|','/').replace('\n',' ')
        lines.append(f'| {did} | {_family(e)} | {_target(e)} | {desc[:120]} |')
    lines += ['', '## 未对齐误杀高频点', '', f'- 误杀事件数：{len(unexpected)}', f'- 涉及样本：{len({x[0] for x in unexpected})}']
    ctr = Counter((e.get('kind'), e.get('node_id') or e.get('knowledge_id') or e.get('constraint_id'), e.get('requirement_id') or e.get('text') or e.get('reason')) for _id, e in unexpected)
    lines += ['', '| 次数 | 类型 | 位置 | 具体 atom / 关系 |', '|---:|---|---|---|']
    for (kind, loc, text), count in ctr.most_common(50):
        lines.append(f'| {count} | {kind} | {loc} | {str(text).replace("|","/")[:120]} |')
    return '\n'.join(lines) + '\n'


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('reports', nargs='+', help='all_reports_merged.json files')
    ap.add_argument('--out', default='', help='optional markdown output')
    args = ap.parse_args()
    chunks = []
    for p in args.reports:
        path = Path(p)
        chunks.append(summarize(_load(path), path.parent.name or path.stem))
    text = '\n\n'.join(chunks)
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
    else:
        print(text)


if __name__ == '__main__':
    main()
