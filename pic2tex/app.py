"""Command line / GUI entry point for Pic2TeX."""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import time
from pathlib import Path

from . import __version__
from .api import DeepSeekError, DeepSeekVisionClient
from .clipboard import ClipboardWatcher
from .config import PROMPT_STYLES, WRAPPERS, Config, app_data_dir
from .imageio import ImageError, load_image_file
from .latex import apply_wrapper
from .prompts import build_prompt

log = logging.getLogger(__name__)


def setup_logging() -> None:
    """Log to ``%APPDATA%\\Pic2TeX\\pic2tex.log`` (GUI has no console)."""
    handlers: list[logging.Handler] = []
    try:
        app_data_dir().mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.FileHandler(app_data_dir() / "pic2tex.log", encoding="utf-8")
        )
    except OSError:
        pass
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    if handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=handlers,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pic2tex",
        description="把截图 / 图片转换成 LaTeX 代码（DeepSeek 视觉模型）。不带参数时打开图形界面。",
        epilog=(
            "示例:\n"
            "  python -m pic2tex --gui\n"
            "  python -m pic2tex formula.png -o formula.tex\n"
            "  python -m pic2tex --watch\n"
            "  python -m pic2tex --check\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("images", nargs="*", help="要转换的图片文件（PNG/JPEG/GIF/WebP/BMP）")
    parser.add_argument("--gui", action="store_true", help="打开图形界面（默认行为）")
    parser.add_argument("--demo", action="store_true", help="以演示模式打开图形界面（不调用 API）")
    parser.add_argument("--watch", action="store_true", help="监听剪贴板，把每次截图识别结果打印到终端")
    parser.add_argument("-o", "--output", help="把结果写入 .tex 文件（CLI 模式）")
    parser.add_argument("--style", choices=PROMPT_STYLES, help="排版风格: math（公式，默认）/ document（整页）")
    parser.add_argument("--wrapper", choices=WRAPPERS, help="包裹方式: display（行间，默认）/ inline / none")
    parser.add_argument("--model", help="模型名称（默认 deepseek-flash）")
    parser.add_argument("--base-url", help="API 地址（默认 https://api.deepseek.com）")
    parser.add_argument("--api-key", help="直接传入 API Key（默认读取配置/环境变量）")
    parser.add_argument("--check", action="store_true", help="检查 API Key 与网络连接")
    parser.add_argument("--version", action="version", version=f"Pic2TeX {__version__}")
    return parser


def apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    if args.style:
        config.prompt_style = args.style
    if args.wrapper:
        config.wrapper = args.wrapper
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key
    return config


def make_client(config: Config, *, timeout: float | None = None) -> DeepSeekVisionClient:
    key = config.effective_api_key
    if not key:
        raise DeepSeekError(
            "尚未配置 DeepSeek API Key：请在图形界面「设置」中填写，"
            "或设置环境变量 DEEPSEEK_API_KEY。",
            kind="config",
        )
    return DeepSeekVisionClient(
        key,
        base_url=config.effective_base_url,
        model=config.effective_model,
        timeout=float(timeout or config.timeout),
        max_retries=2,
    )


def cmd_check(config: Config) -> int:
    print(f"Pic2TeX {__version__}")
    print(f"API 地址 : {config.effective_base_url}")
    print(f"模型     : {config.effective_model}")
    print(f"API Key  : {config.masked_key()}（来源：{config.api_key_source}）")
    if not config.effective_api_key:
        print("✗ 未配置 API Key。请在图形界面设置中填写，或设置 DEEPSEEK_API_KEY。")
        return 2
    print("正在连接 DeepSeek …")
    try:
        client = make_client(config, timeout=30)
        ok, message = client.check_connection()
    except DeepSeekError as exc:
        print(f"✗ {exc}")
        return 1
    print(("✓ " if ok else "✗ ") + message)
    return 0 if ok else 1


def convert_images(config: Config, paths: list[str], output: str | None) -> int:
    try:
        client = make_client(config)
    except DeepSeekError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    prompt = build_prompt(config.prompt_style)
    chunks: list[str] = []
    failures = 0
    for raw_path in paths:
        path = Path(raw_path)
        try:
            image = load_image_file(path, source="file")
        except (ImageError, OSError) as exc:
            print(f"✗ 无法读取 {path}: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(f"→ 正在识别 {path.name}（{image.width}×{image.height}）…", file=sys.stderr)
        started = time.monotonic()
        try:
            # CLI mode prints the cleaned result once, so no streaming here.
            result = client.convert(
                image,
                prompt,
                max_tokens=int(config.max_tokens),
                temperature=float(config.temperature),
                detail=config.detail,
                stream=False,
            )
        except DeepSeekError as exc:
            print(f"\n✗ 识别失败：{exc}", file=sys.stderr)
            failures += 1
            continue
        latex = apply_wrapper(result.text, config.wrapper)
        elapsed = time.monotonic() - started
        print(f"✓ {path.name} 完成（{elapsed:.1f}s）", file=sys.stderr)
        chunks.append(latex)

    if output:
        header = f"% Generated by Pic2TeX {__version__} ({config.effective_model})\n"
        body = "\n\n".join(chunks)
        try:
            Path(output).write_text(header + body + "\n", encoding="utf-8")
            print(f"已写入 {output}", file=sys.stderr)
        except OSError as exc:
            print(f"✗ 无法写入 {output}: {exc}", file=sys.stderr)
            return 1
    else:
        if chunks:
            print("\n\n".join(chunks))
    return 1 if failures else 0


def cmd_watch(config: Config) -> int:
    try:
        client = make_client(config)
    except DeepSeekError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    prompt = build_prompt(config.prompt_style)
    payloads: queue.Queue = queue.Queue()
    errors: queue.Queue = queue.Queue()

    def on_image(image) -> None:
        payloads.put(image)

    def on_error(exc: Exception) -> None:
        errors.put(exc)

    watcher = ClipboardWatcher(on_image, on_error, poll_interval=0.35, start_with_current=False)
    watcher.start()
    print("正在监听剪贴板：按 Win+Shift+S 截图即可识别，按 Ctrl+C 退出。", file=sys.stderr)
    try:
        while True:
            try:
                image = payloads.get(timeout=0.4)
            except queue.Empty:
                try:
                    exc = errors.get_nowait()
                except queue.Empty:
                    continue
                print(f"剪贴板错误：{exc}", file=sys.stderr)
                continue
            started = time.monotonic()
            print(f"→ 检测到截图 {image.width}×{image.height}，识别中…", file=sys.stderr)
            try:
                result = client.convert(
                    image,
                    prompt,
                    max_tokens=int(config.max_tokens),
                    temperature=float(config.temperature),
                    detail=config.detail,
                    stream=False,
                )
            except DeepSeekError as exc:
                print(f"✗ 识别失败：{exc}", file=sys.stderr)
                continue
            latex = apply_wrapper(result.text, config.wrapper)
            print(latex, flush=True)
            print(flush=True)
            print(f"✓ 完成（{time.monotonic() - started:.1f}s）", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("\n已退出监听。", file=sys.stderr)
        return 0
    finally:
        watcher.stop()


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    config = apply_overrides(Config.load(), args)

    if args.check:
        return cmd_check(config)

    if args.watch:
        return cmd_watch(config)

    if args.output and not args.images:
        print("错误：-o/--output 需要至少一个图片文件。", file=sys.stderr)
        return 2

    use_gui = args.gui or (not args.images and not args.output)
    if use_gui:
        from .gui import run_gui

        return run_gui(config, demo=args.demo, initial_images=args.images or None)

    return convert_images(config, args.images, args.output)
