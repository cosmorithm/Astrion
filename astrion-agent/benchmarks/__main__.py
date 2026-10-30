"""benchmark 命令行入口；目前只正式接入 BFCL。"""

from __future__ import annotations

import argparse

from . import bfcl


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Astrion Agent 外部官方 benchmark 适配入口")
    parser.add_argument("backend", choices=["bfcl"], help="要运行的官方 benchmark")
    args, remaining = parser.parse_known_args(argv)
    if args.backend == "bfcl":
        return bfcl.main(remaining)
    raise AssertionError("不可达分支")


if __name__ == "__main__":
    raise SystemExit(main())
