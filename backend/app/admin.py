"""Offline account administration; passwords are entered without terminal echo."""
from __future__ import annotations

import argparse
import getpass
import sqlite3

from .auth_store import AuthStore, normalize_username
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create-user", "set-password", "disable-user", "claim-local-projects"))
    parser.add_argument("username")
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.prepare()
    store = AuthStore(settings.database_path)
    store.initialize()
    try:
        if args.command in {"create-user", "set-password"}:
            password = getpass.getpass("Password (12–256 characters): ")
            if password != getpass.getpass("Repeat password: "):
                raise ValueError("Passwords do not match.")
            if args.command == "create-user":
                user = store.create_user(args.username,password)
                print(f"Created account {user['username']} ({user['id']}).")
            else:
                store.set_password(args.username,password)
                print("Password changed; all existing sessions revoked.")
        elif args.command == "disable-user":
            store.disable_user(args.username)
            print("Account disabled and all sessions revoked. Recordings are retained until explicitly deleted.")
        else:
            user = store.user(normalize_username(args.username))
            if not user or user["disabled"]:
                raise ValueError("An active account is required.")
            # Deliberate operator action. Hosted requests can never auto-claim
            # recordings created by the pre-authentication local workspace.
            with store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                changed = conn.execute("UPDATE projects SET owner_id=? WHERE owner_id='local'", (user["id"],)).rowcount
            print(f"Assigned {changed} local projects to {user['username']}. Stop API/worker before migration and retain a backup.")
    except (ValueError, sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        parser.exit(1, f"Account operation failed: {exc}\n")


if __name__ == "__main__":
    main()
