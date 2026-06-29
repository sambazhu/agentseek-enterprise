from __future__ import annotations

import json


def make_text(content: str) -> str:
    return json.dumps(
        {
            "msgtype": "text",
            "text": {
                "content": content,
            },
        },
        ensure_ascii=False,
    )


def make_text_stream(stream_id: str, content: str, finish: bool) -> str:
    return json.dumps(
        {
            "msgtype": "stream",
            "stream": {
                "id": stream_id,
                "finish": finish,
                "content": content,
            },
        },
        ensure_ascii=False,
    )
