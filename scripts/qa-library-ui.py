from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from company_platform.server import create_server


def find_edge() -> Path:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("未找到 Microsoft Edge，无法执行资料库截图巡检")


def main() -> int:
    parser = argparse.ArgumentParser(description="使用合成资料渲染公司资料库并截图")
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="agent4company-library-qa-", ignore_cleanup_errors=True
    ) as temporary:
        runtime = Path(temporary) / "runtime"
        server = create_server(project_root=root, runtime_dir=runtime, port=0)
        library = server.context.library
        library.import_document(
            "startup-bp.md",
            "# 公司融资 BP\n核心产品用于工业研发协作，计划完成种子轮融资。".encode(),
            title="公司融资 BP",
            category="bp",
            tags=["融资", "战略"],
            version_note="第一版",
        )
        library.import_document(
            "invention-patent.txt",
            "发明名称：多轴电机控制方法。核心权利要求涉及实时控制与安全降级。".encode(),
            title="多轴电机控制发明专利",
            category="patent",
            tags=["核心技术"],
        )
        library.import_document(
            "architecture.md",
            "# 桌面平台架构\n采用本地服务、受控 DAG 与不可变资料版本。".encode(),
            title="桌面平台开发文档",
            category="development",
            tags=["架构", "V1"],
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/#knowledge"
        try:
            with urlopen(url.removesuffix("#knowledge") + "api/health", timeout=5) as response:
                if json.loads(response.read())["status"] != "ok":
                    raise RuntimeError("工作台健康检查失败")
            command = [
                str(find_edge()),
                "--headless=new",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-extensions",
                "--hide-scrollbars",
                "--no-default-browser-check",
                "--no-first-run",
                "--window-size=1440,900",
                "--force-device-scale-factor=1",
                "--virtual-time-budget=6000",
                f"--user-data-dir={Path(temporary) / 'edge-profile'}",
                f"--screenshot={output}",
                url,
            ]
            previous_mtime = output.stat().st_mtime_ns if output.exists() else 0
            browser = subprocess.Popen(
                command,
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if (
                        output.is_file()
                        and output.stat().st_size > 0
                        and output.stat().st_mtime_ns > previous_mtime
                    ):
                        break
                    if browser.poll() is not None:
                        break
                    time.sleep(0.2)
                if (
                    not output.is_file()
                    or output.stat().st_size == 0
                    or output.stat().st_mtime_ns <= previous_mtime
                ):
                    raise RuntimeError("Edge 未在 30 秒内生成资料库截图")
            finally:
                if browser.poll() is None:
                    subprocess.run(
                        ["taskkill", "/PID", str(browser.pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    try:
                        browser.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    time.sleep(1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
    print(
        json.dumps(
            {"status": "ok", "screenshot": str(output), "bytes": output.stat().st_size},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
