# agentseek-wecom

## At A Glance

| Item | Value |
| --- | --- |
| Distribution | `agentseek-wecom` |
| Python package | `agentseek_wecom` |
| Bub entry point | `wecom` |
| Workspace path | `contrib/agentseek-wecom` |
| Test target | `make test-wecom` |
| Type check target | `make typecheck-wecom` |

## When To Use It

Use this plugin when AgentSeek should receive messages from an Enterprise WeChat intelligent robot. It owns only the channel protocol:

- AI Bot HTTP Callback verification, encryption, polling streams, and `response_url` delivery;
- AI Bot WebSocket subscription, heartbeat, reconnect, and active stream delivery;
- text, voice, image, file, video, mixed, quoted, and card-event input;
- encrypted durable inbox, msgid deduplication, outbox, and proactive eligibility;
- `enter_chat` welcome events and group/direct-message isolation.

Employee identity lookup belongs to `agentseek-enterprise`, which reads `from_userid` / `oa_account` from the message state. Business workflows such as meeting room booking and travel requests should remain MCP tools.

## Configure Callback

```env
AGENTSEEK_WECOM_ENABLED=true
AGENTSEEK_WECOM_TRANSPORT_MODE=callback
AGENTSEEK_WECOM_HOST=0.0.0.0
AGENTSEEK_WECOM_PORT=12000
AGENTSEEK_WECOM_CALLBACK_PATH=/ai-bot/callback/demo/<botid>
AGENTSEEK_WECOM_TOKEN=
AGENTSEEK_WECOM_ENCODING_AES_KEY=
```

For intelligent robot callbacks, `AGENTSEEK_WECOM_RECEIVE_ID` should normally stay empty.

## Configure Long Connection

The WeCom admin console permits one API mode per AI Bot. Switching to long
connection disables the Callback URL. Use the long-connection-specific Secret,
not the Callback Token or EncodingAESKey.

```env
AGENTSEEK_WECOM_ENABLED=true
AGENTSEEK_WECOM_TRANSPORT_MODE=long_connection
AGENTSEEK_WECOM_LONG_CONNECTION_BOT_ID=
AGENTSEEK_WECOM_LONG_CONNECTION_SECRET=
AGENTSEEK_WECOM_LONG_CONNECTION_URL=wss://openws.work.weixin.qq.com
AGENTSEEK_WECOM_LONG_CONNECTION_HEARTBEAT_SECONDS=30
AGENTSEEK_WECOM_LONG_CONNECTION_LOCK_PATH=runtime/wecom-long-connection.lock
AGENTSEEK_WECOM_DURABLE_MODE=sqlite
AGENTSEEK_WECOM_DURABLE_SQLITE_PATH=runtime/wecom-messages.sqlite3
AGENTSEEK_WECOM_DURABLE_SECRET=
# Keep empty outside an explicit live-verification window.
AGENTSEEK_WECOM_LONG_CONNECTION_PROACTIVE_PROBE_TRIGGER=
```

Long connection keeps `AGENTSEEK_WECOM_HOST` and `AGENTSEEK_WECOM_PORT` for
the local `/health` endpoint. A healthy subscribed instance reports
`transport="long_connection"` and `subscribed=true`.

If the robot creator is not an enterprise super administrator, `from.userid` can be an encrypted robot `open_userid`. Configure a self-built app that is visible to the target users and enable the official conversion API:

```env
AGENTSEEK_WECOM_CORP_ID=
AGENTSEEK_WECOM_APP_SECRET=
AGENTSEEK_WECOM_API_BASE_URL=https://qyapi.weixin.qq.com
AGENTSEEK_WECOM_USERID_RESOLVE_MODE=openuserid_to_userid
AGENTSEEK_WECOM_USERID_CACHE_TTL_SECONDS=3600
```

When enabled, the channel keeps the original callback value in `context.from_userid` / `context.wecom.open_userid`, and writes the converted plaintext userid to `context.userid` and `context.oa_account`.

## Run

Enable the channel through Bub's channel runner with the plugins group installed:

```bash
uv sync --group plugins
uv run agentseek gateway --enable-channel wecom
```

Callback serves the configured callback path. Long connection serves only the
local health and optional signed-artifact routes on that HTTP listener.

## Runtime Behavior

On inbound AI Bot messages, the channel:

1. authenticates the selected transport and normalizes a `ConversationAddress`;
2. durably admits and deduplicates the inbound `msgid` when SQLite mode is enabled;
3. creates a WeCom `stream.id` and preserves direct/group session isolation;
4. resolves encrypted robot userids when configured;
5. emits a Bub `ChannelMessage` with `channel="wecom"`;
6. delivers stream updates through Callback polling or active WebSocket commands.

Long-connection replies retain the callback `req_id`, have a 24-hour reply
deadline, and must finish their stream within WeCom's ten-minute stream window.
`send_proactive_markdown()` and `send_proactive_template_card()` require an
idempotency key and an address previously observed from that robot.

After a process restart, an unfinished inbox no longer reuses the previous
connection's stream callback. Its terminal result uses durable, idempotent
proactive Markdown. A recovered stream outbox first retries its original stream;
if WeCom explicitly rejects that command, the channel falls back to the same
proactive path. Timeouts and other ambiguous outcomes do not trigger fallback.

Long-connection card clicks enter the same session queue. Their terminal Agent
result uses idempotent proactive Markdown; it is not sent with the message-only
`aibot_respond_msg` command.

Inbound messages keep a rich internal context for routing, identity resolution,
media handling, deduplication, and idempotency. The model-visible context is a
separate semantic projection. It excludes `msgid`, BotID, raw chatid, userid,
reply capabilities, and signed media data.

## Verify

```bash
make test-wecom
make typecheck-wecom
```

## Limitations

- WeCom permits only one effective WebSocket connection per AI Bot. The plugin also holds a local process lock.
- M0.5 does not implement the official chunked media-upload commands. Direct file delivery remains disabled.
- Proactive AI Bot delivery targets only conversations that already interacted with the robot. It cannot target an arbitrary employee.
- The encrypted SQLite store is single-host. Multi-host active/active deployment needs a shared durable adapter.
- Self-built application transport is planned separately; it is required for arbitrary member, department, or tag notifications.
