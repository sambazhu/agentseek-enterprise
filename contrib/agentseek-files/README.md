# agentseek-files

`agentseek-files` provides the file layer for enterprise AgentSeek deployments.

The package is channel-agnostic. A channel plugin such as `agentseek-wecom`
downloads uploaded media, then passes the file bytes and scope metadata to this
package. The package stores the file under a scoped runtime directory, extracts
safe text when configured, and builds model-facing context blocks without
exposing host filesystem paths.

The first implementation target is v0.0.9 of the Enterprise WeCom digital
employee template.

The optional `workspace_download` module issues short-lived browser links to
scoped outbound `summary.csv` files. It checks ownership when issuing a link,
stores only the token hash in a private SQLite database, and checks the stored
file's scope, expiry, size and SHA256 on each download. Possession of a link
grants access until expiry; it is not a second user login. Repeated downloads
within the lifetime are supported. Expired links can be reissued by the trusted
application without rerunning the computation.

`agentseek-wecom` can host the HTTP routes on its existing ASGI app. This uses no
WeCom media upload/send API and is independent of Work report delivery. The
feature is disabled unless `AGENTSEEK_WORKSPACE_DOWNLOAD_MODE=signed_link`.
The HTTPS base URL must end exactly in `/ai-server/workspace-files`; configure
`AGENTSEEK_WORKSPACE_DOWNLOAD_BASE_URL`, an existing absolute mode-0700 directory
in `AGENTSEEK_WORKSPACE_DOWNLOAD_GRANTS_DIR`, and optionally
`AGENTSEEK_WORKSPACE_DOWNLOAD_TTL_SECONDS` (default 600, maximum 3600).

The URL fragment contains the token. The browser clears the fragment and posts
the token to the download endpoint when the user clicks the download button;
the token is not part of proxy URL logs. Do not log full links or POST bodies.
Reverse-proxy HTTPS access and opening the downloaded file on the user's device
still need deployment validation.
