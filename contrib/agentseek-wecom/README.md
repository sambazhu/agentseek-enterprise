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

Use this plugin when AgentSeek should receive messages from an Enterprise WeChat intelligent robot callback. It owns only the channel protocol:

- VerifyURL handshake;
- JSON AES/SHA1 decrypt and encrypt;
- text / voice inbound messages;
- stream polling responses;
- `enter_chat` welcome event.

Employee identity lookup belongs to `agentseek-enterprise`, which reads `from_userid` / `oa_account` from the message state. Business workflows such as meeting room booking and travel requests should remain MCP tools.

## Configure

```env
AGENTSEEK_WECOM_ENABLED=true
AGENTSEEK_WECOM_HOST=0.0.0.0
AGENTSEEK_WECOM_PORT=12000
AGENTSEEK_WECOM_CALLBACK_PATH=/ai-bot/callback/demo/<botid>
AGENTSEEK_WECOM_TOKEN=
AGENTSEEK_WECOM_ENCODING_AES_KEY=
```

For intelligent robot callbacks, `AGENTSEEK_WECOM_RECEIVE_ID` should normally stay empty.

## Run

Enable the channel through Bub's channel runner with the plugins group installed:

```bash
uv sync --group plugins
uv run agentseek gateway --enable-channel wecom
```

The callback endpoint is served from the configured host, port, and path.

## Runtime Behavior

On text or voice callbacks, the channel:

1. decrypts the callback body;
2. creates a WeCom `stream.id`;
3. emits a Bub `ChannelMessage` with `channel="wecom"` and `context.oa_account` set to the WeCom userid;
4. returns an encrypted `msgtype=stream` response;
5. updates the stream content when the model output is sent back through `channel.send()`.

When Bub is run with stream output enabled, `stream_events()` also updates the current stream incrementally.

## Verify

```bash
make test-wecom
make typecheck-wecom
```

## Limitations

- The first implementation supports text, voice, stream polling, and `enter_chat`.
- Image and mixed-message download/decryption are intentionally left for a later channel extension.
- Stream state is in-process with TTL cleanup; use a shared store if multiple callback replicas are deployed.
