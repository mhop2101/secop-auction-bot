"""``python -m secop_web serve`` and ``python -m secop_web make-user``."""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import List, Optional

from . import auth
from .server import serve


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="secop-web", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("serve", help="run the web app")
    run.add_argument("--data", default="webjobs", help="where jobs are kept")
    run.add_argument("--users", default="secrets/webapp_users.json", help="login file")
    run.add_argument("--host", default="0.0.0.0")
    run.add_argument("--port", type=int, default=443, help="HTTPS port (HTTP without --cert)")
    run.add_argument("--http-port", type=int, default=80,
                     help="plain-HTTP port that only redirects to HTTPS; 0 to disable")
    run.add_argument("--cert", default=None, help="TLS certificate (PEM)")
    run.add_argument("--key", default=None, help="TLS private key (PEM)")

    user = sub.add_parser("make-user", help="write the login file")
    user.add_argument("--users", default="secrets/webapp_users.json")
    user.add_argument("--username", required=True)
    user.add_argument("--password-file", default=None,
                      help="read the password from this file instead of prompting")

    args = parser.parse_args(argv)
    if args.command == "serve":
        serve(args.data, args.users, args.host, args.port, args.http_port or None,
              args.cert, args.key)
        return 0
    if args.command == "make-user":
        if args.password_file:
            with open(args.password_file, encoding="utf-8") as handle:
                password = handle.read().strip()
        else:
            password = getpass.getpass("password: ")
        if len(password) < 12:
            print("password must be at least 12 characters", file=sys.stderr)
            return 2
        auth.write_user_file(args.users, args.username, password)
        print("wrote %s" % args.users)
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
