"""在 VibeCheck-AI 后端进程内调用真实扫描器。

本脚本只使用标准库，由 VibeCheck-AI 自带的虚拟环境解释器执行，
因此不会把上游依赖引入本项目的运行环境。输出字段经过白名单筛选，
扫描器自身的脱敏字段 snippet_masked 是唯一片段来源。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _finding(item) -> dict:
    return {
        "rule_id": item.rule_id,
        "rule_name": item.rule_name,
        "severity": item.severity.value,
        "confidence": item.confidence.value,
        "dimension": item.dimension,
        "file_path": item.file_path,
        "line_start": item.line_start,
        "line_end": item.line_end,
        "column_start": item.column_start,
        "column_end": item.column_end,
        "snippet_masked": item.snippet_masked,
        "is_blocking": item.is_blocking,
        "finding_type": item.finding_type.value,
        "category": item.category,
        "secret_type": item.secret_type,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: _vibecheck_scan.py <scan_root> <output.json>", file=sys.stderr)
        return 2
    scan_root = Path(argv[0]).resolve()
    output = Path(argv[1])
    # 以 VibeCheck-AI backend 为工作目录启动时，app 包位于当前目录。
    sys.path.insert(0, str(Path.cwd()))

    from app.scanner.sensitive import scan_directory

    result = scan_directory(scan_root)
    payload = {
        "scanner": {
            "module": "app.scanner.sensitive",
            "function": "scan_directory",
        },
        "scan_root": str(scan_root),
        "total_files_scanned": result.total_files_scanned,
        "total_lines_scanned": result.total_lines_scanned,
        "findings": [_finding(item) for item in result.findings],
        "notices": [
            {
                "rule_id": item.rule_id,
                "message": item.message,
                "file_path": item.file_path,
            }
            for item in result.notices
        ],
        "skipped_files": [
            {"file_path": item.file_path, "reason": item.reason}
            for item in result.skipped_files
        ],
        "scan_errors": [
            {
                "file_path": item.file_path,
                "error_type": item.error_type,
                "error_message": item.error_message,
            }
            for item in result.scan_errors
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:  # noqa: BLE001 - 只向上游返回失败状态
        print(f"SCAN_FAILED: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
