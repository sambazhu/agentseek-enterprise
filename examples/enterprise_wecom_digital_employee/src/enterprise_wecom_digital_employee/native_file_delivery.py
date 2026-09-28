"""Native file tools, independent of sandbox spec, grants and broker."""

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from enterprise_wecom_digital_employee.sandbox_authorization import runtime_scope


def native_file_tools():
    def binding(runtime):
        from agentseek_wecom.file_delivery import STATE_KEY, resolve_capability

        return resolve_capability(runtime.state.get(STATE_KEY), runtime_scope(runtime))

    @tool
    def list_workspace_delivery_files(runtime: ToolRuntime) -> dict:
        """List saved outbound files in this authenticated conversation, without sending.

        Never assume the latest file. For ambiguous filenames, ask the user to
        select a returned file_ref. Expired or inaccessible files cannot be sent.
        """
        try:
            return {"status": "available", "files": binding(runtime).list_files()}
        except Exception:
            return {"status": "unavailable", "files": []}

    @tool
    async def deliver_workspace_file(file_ref: str, runtime: ToolRuntime) -> dict:
        """Send one existing file to the authenticated employee, only on explicit request.

        First list files; use its exact file_ref. The server accepts a latest
        message such as '把 summary.csv 发给我' or '发送工作区文件 <file_ref>'.
        For another send the USER must explicitly say '重发工作区文件 <file_ref>'
        or '把 summary.csv 再发给我'. Never invent confirmation, recipients or keys.
        api_accepted is NOT user receipt/opening. uncertain must not be retried.
        This tool never executes a sandbox, renews retention or generates a file.
        """
        try:
            return await binding(runtime).deliver(file_ref)
        except Exception:
            return {
                "status": "denied",
                "api_accepted": False,
                "user_receipt_confirmed": False,
                "automatic_retry_allowed": False,
                "guidance": "请确认唯一文件，并明确说：发送工作区文件 <file_ref>。不确定结果不得自动重发。",
            }

    return [list_workspace_delivery_files, deliver_workspace_file]
