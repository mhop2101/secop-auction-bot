"""secop_web — a small authenticated web front end for the secop_firstbid bot.

It does not bid by itself. It writes a bot config from a form, runs the bot's own
commands (``parse-page``, ``preflight``, ``clock-probe``, ``run``) as subprocesses at the
right moments, and shows what they printed and what SECOP recorded. Everything the bot
guarantees — one connection per bid, no retry at the fire instant, ``--yes-i-mean-it`` as
the only thing that arms a live ``CreateBid`` — is the bot's, unchanged.

Standard library only, like the bot: the server is ``http.server`` behind ``ssl``.
"""

from __future__ import annotations

__version__ = "1.0.0"
