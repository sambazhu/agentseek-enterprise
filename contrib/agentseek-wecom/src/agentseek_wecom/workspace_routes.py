"""Mount browser access to persistent workspace CSV files on the gateway app.

No WeCom media upload/send API, Work repository, or guest executor is called.
"""

import asyncio
import re
import secrets

from fastapi import HTTPException, Request
from fastapi.responses import Response


def register_workspace_routes(app, downloads):
    from agentseek_files.workspace_download import (
        ROUTE_PATH,
        WorkspaceDownloadExpired,
        WorkspaceDownloadNotFound,
    )

    @app.get(ROUTE_PATH + "/{link_id}", include_in_schema=False)
    async def workspace_page(link_id: str):
        if not re.fullmatch(r"workspace_[a-f0-9]{64}", link_id):
            raise HTTPException(404, "workspace link unavailable")
        nonce = secrets.token_hex(16)
        page = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>工作区文件</title>
</head><body><h1>summary.csv</h1><p id="status">点击下载已保存的工作区文件。</p>
<button id="download" type="button">下载 summary.csv</button><script nonce="NONCE">
const token=window.location.hash.slice(1);history.replaceState(null,'',window.location.pathname);
const button=document.getElementById('download'),status=document.getElementById('status');
if(!token){button.disabled=true;status.textContent='下载链接无效，请在原会话查询结果以获取新链接。';}
button.onclick=async()=>{button.disabled=true;try{
const response=await fetch(window.location.pathname+'/redeem',
{method:'POST',headers:{'Content-Type':'text/plain'},body:token,credentials:'omit',cache:'no-store'});
if(!response.ok)throw new Error('unavailable');
const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');
link.href=url;link.download='summary.csv';document.body.appendChild(link);link.click();link.remove();
setTimeout(()=>URL.revokeObjectURL(url),60000);
status.textContent='下载已开始，请打开文件核对结果。';
}catch(error){status.textContent='文件或链接不可用，请在原会话查询结果；无需重新执行任务。';}
finally{button.disabled=false;}};
</script></body></html>""".replace("NONCE", nonce)
        return Response(page, media_type="text/html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; connect-src 'self'; "
                f"script-src 'nonce-{nonce}'; base-uri 'none'; frame-ancestors 'none'",
        })

    @app.post(ROUTE_PATH + "/{link_id}/redeem", include_in_schema=False)
    async def redeem_workspace(link_id: str, request: Request):
        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > 128:
                raise HTTPException(404, "workspace link unavailable")
            body.extend(part)
        try:
            data = await asyncio.to_thread(downloads.redeem, link_id, body.decode("ascii"))
        except WorkspaceDownloadExpired as exc:
            raise HTTPException(410, "workspace link expired") from exc
        except (WorkspaceDownloadNotFound, ValueError, OSError, KeyError, TypeError) as exc:
            raise HTTPException(404, "workspace link unavailable") from exc
        return Response(data, media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="summary.csv"',
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        })
