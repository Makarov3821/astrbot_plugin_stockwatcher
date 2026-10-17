"""Read-only real API diagnostic: python -m stockwatcher.probe --symbol MU."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from .config import Settings
from .domain import ValidationError
from .market import MarketService
from .provider import AlpacaProvider, MarketError
from .storage import Store


async def run(symbol: str) -> tuple[dict, int]:
    settings = Settings.from_mapping()
    provider = AlpacaProvider(settings)
    try:
        with tempfile.TemporaryDirectory(prefix="stockwatcher-probe-") as directory:
            store = Store(Path(directory) / "probe.sqlite3")
            try:
                result = await MarketService(provider, store).diagnose(symbol)
                valid = bool(
                    result["daily_count"]
                    and result["adjusted_daily_count"]
                    and result["regular_minute_count"]
                )
                return {
                    "status": "reachable" if valid else "incomplete",
                    **result,
                }, 0 if valid else 1
            finally:
                store.close()
    except MarketError as exc:
        return {"status": "not_verified", "code": exc.code, "message": str(exc)}, 2
    except ValidationError as exc:
        return {"status": "invalid_input", "message": str(exc)}, 2
    finally:
        await provider.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只读验证 Alpaca 免费 SIP 历史行情及交易日历，不提交交易。"
    )
    parser.add_argument("--symbol", default="MU")
    args = parser.parse_args()
    try:
        result, status = asyncio.run(run(args.symbol))
    except ValidationError as exc:
        result, status = {"status": "invalid_config", "message": str(exc)}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
