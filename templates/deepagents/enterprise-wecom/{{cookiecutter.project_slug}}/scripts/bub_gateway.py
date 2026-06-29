"""Start Bub while making optional Logfire instrumentation non-fatal.

Bub 0.3.9 configures Logfire at import time. When logfire is installed but no
token is configured, that import can fail before the gateway starts. The
enterprise gateway treats Logfire as optional, so this wrapper downgrades that
case to local-only instrumentation.
"""

from __future__ import annotations


def _guard_logfire_configure() -> None:
    try:
        import logfire
        from logfire.exceptions import LogfireConfigError
    except ImportError:
        return

    original_configure = logfire.configure

    def configure_without_required_token(*args: object, **kwargs: object) -> object:
        try:
            return original_configure(*args, **kwargs)
        except LogfireConfigError:
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["send_to_logfire"] = False
            return original_configure(*args, **fallback_kwargs)

    logfire.configure = configure_without_required_token


def main() -> None:
    _guard_logfire_configure()

    from bub.__main__ import app

    app()


if __name__ == "__main__":
    main()
