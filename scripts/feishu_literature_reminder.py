#!/usr/bin/env python3
"""Send a daily literature-reading reminder to a Feishu group bot.

The Feishu webhook is intentionally read from the FEISHU_WEBHOOK
environment variable instead of being hard-coded in this file.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


BEIJING_TZ = timezone(timedelta(hours=8))


def u(text: str) -> str:
    """Decode escaped Unicode literals while keeping this source ASCII-safe."""
    return text.encode("ascii").decode("unicode_escape")


def build_message() -> str:
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    return "\n".join(
        [
            u(f"\\U0001f4da \\u6587\\u732e\\u9605\\u8bfb\\u63d0\\u9192 | {today} | \\u6bcf\\u65e5\\u6587\\u732e"),
            "",
            u(
                "\\u7814\\u7a76\\u4e3b\\u9898\\uff1a"
                "\\u4e0d\\u540c\\u57fa\\u56e0\\u578b\\u5916\\u751f\\u83cc\\u6839\\u771f\\u83cc"
                "\\u8c03\\u63a7\\u9a6c\\u5c3e\\u677e\\u5e72\\u65f1\\u80c1\\u8feb"
                "\\u4e0e\\u590d\\u6c34\\u6062\\u590d\\u7814\\u7a76"
            ),
            "",
            u("\\u4eca\\u65e5\\u68c0\\u7d22\\u5173\\u952e\\u8bcd\\uff1a"),
            (
                "ectomycorrhizal fungi; Cenococcum geophilum; Pinus massoniana; "
                "drought stress; water use efficiency; non-structural carbohydrates; "
                "re-watering recovery"
            ),
            "",
            u("\\u5efa\\u8bae\\u9605\\u8bfb\\u91cd\\u70b9\\uff1a"),
            u("1. \\u662f\\u5426\\u6d89\\u53ca ECM / Cg / Suillus \\u4e0e\\u677e\\u5c5e\\u690d\\u7269\\u5e72\\u65f1\\u54cd\\u5e94"),
            u(
                "2. \\u662f\\u5426\\u5305\\u542b WUE\\u3001RWC\\u3001NSC\\u3001SS/St\\u3001"
                "Pn\\u3001Gs\\u3001Tr\\u3001Ci \\u7b49\\u6307\\u6807"
            ),
            u("3. \\u662f\\u5426\\u8ba8\\u8bba\\u5e72\\u65f1\\u540e\\u590d\\u6c34\\u6062\\u590d\\u6216\\u78b3\\u6c34\\u5206\\u914d\\u673a\\u5236"),
            "",
            u("\\u5f52\\u6863\\u76ee\\u5f55\\uff1aC:/r/\\u6838\\u5fc3\\u6587\\u732e\\u4e0b\\u8f7d"),
        ]
    )


def send_to_feishu(webhook: str, text: str) -> None:
    payload = {
        "msg_type": "text",
        "content": {
            "text": text,
        },
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
            if response.status >= 400:
                raise RuntimeError(f"Feishu returned HTTP {response.status}: {body}")
            print(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Feishu returned HTTP {exc.code}: {body}") from exc


def main() -> int:
    webhook = os.environ.get("FEISHU_WEBHOOK")
    if not webhook:
        print("Missing FEISHU_WEBHOOK environment variable.", file=sys.stderr)
        return 2

    send_to_feishu(webhook, build_message())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
