# agentseek-files

`agentseek-files` provides the file layer for enterprise AgentSeek deployments.

The package is channel-agnostic. A channel plugin such as `agentseek-wecom`
downloads uploaded media, then passes the file bytes and scope metadata to this
package. The package stores the file under a scoped runtime directory, extracts
safe text when configured, and builds model-facing context blocks without
exposing host filesystem paths.

The first implementation target is v0.0.9 of the Enterprise WeCom digital
employee template.
