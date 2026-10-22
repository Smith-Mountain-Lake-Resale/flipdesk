"""Break-glass admin commands.

    docker exec -it flipdesk python -m app.cli list-users
    docker exec -it flipdesk python -m app.cli reset-mfa <username>
    docker exec -it flipdesk python -m app.cli reset-passkeys <username>
    docker exec -it flipdesk python -m app.cli reset-password <username> [new-password]
    docker exec -it flipdesk python -m app.cli make-admin <username>
    docker exec -it flipdesk python -m app.cli enable <username>
    docker exec -it flipdesk python -m app.cli set <setting> <value>      e.g. set mfa_required false
    docker exec -it flipdesk python -m app.cli create-user <username> [--admin]
"""
from __future__ import annotations

import getpass
import secrets
import sys

from sqlalchemy import func

from . import settings
from .db import SessionLocal, init_db
from .models import User
from .services import authn
from .util import hash_password


def _user(db, name: str) -> User:
    u = db.query(User).filter(func.lower(User.username) == name.lower()).first()
    if not u:
        sys.exit(f"No user named {name!r}. Try: list-users")
    return u


def _password(argv: list[str], i: int) -> str:
    if len(argv) > i:
        return argv[i]
    pw = getpass.getpass("New password (blank = generate): ")
    if not pw:
        pw = secrets.token_urlsafe(12)
        print(f"Generated password: {pw}")
    return pw


def main(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    init_db()
    db = SessionLocal()
    cmd = argv[0]
    try:
        if cmd == "list-users":
            for u in db.query(User).order_by(User.id):
                print(f"{u.id:>3}  {u.username:<24} {'admin' if u.is_admin else 'user ':<5}  "
                      f"{'disabled' if u.is_active is False else 'active  '}  totp={'on ' if u.totp_enabled else 'off'}  passkeys={u.passkey_count or 0}  "
                      f"{'sso' if u.oidc_sub else ''}{' password' if u.has_password else ''}  {u.email or ''}")
        elif cmd == "reset-mfa":
            u = _user(db, argv[1])
            authn.disable_totp(db, u)
            authn.sign_out_everywhere(u)
            db.commit()
            print(f"Two-factor removed for {u.username}.")
        elif cmd == "reset-passkeys":
            from .services import passkeys
            u = _user(db, argv[1])
            n = passkeys.remove_all(db, u)
            authn.sign_out_everywhere(u)
            db.commit()
            print(f"Removed {n} passkey(s) from {u.username}.")
        elif cmd == "reset-password":
            u = _user(db, argv[1])
            u.password_hash = hash_password(_password(argv, 2))
            u.is_active = True
            authn.sign_out_everywhere(u)
            db.commit()
            print(f"Password reset for {u.username}.")
        elif cmd == "make-admin":
            u = _user(db, argv[1])
            u.is_admin, u.is_active = True, True
            db.commit()
            print(f"{u.username} is now an admin.")
        elif cmd == "enable":
            u = _user(db, argv[1])
            u.is_active = True
            db.commit()
            print(f"{u.username} enabled.")
        elif cmd == "create-user":
            name = argv[1]
            if db.query(User).filter(func.lower(User.username) == name.lower()).first():
                sys.exit("User exists.")
            pw = _password([a for a in argv if a != "--admin"], 2)
            db.add(User(username=name, password_hash=hash_password(pw), is_admin="--admin" in argv,
                        auth_source="local", is_active=True))
            db.commit()
            print(f"Created {name}.")
        elif cmd == "set":
            key, value = argv[1].upper(), argv[2]
            if key not in settings.REGISTRY:
                sys.exit(f"Unknown setting {key}")
            if settings.locked_by(key):
                print(f"Note: {key} is also set by {settings.locked_by(key)}, which takes precedence.")
            settings.set_one(db, key, value)
            print(f"{key} = {value} (restart not needed)")
        else:
            sys.exit(f"Unknown command {cmd!r}\n{__doc__}")
    except IndexError:
        sys.exit(__doc__)
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv[1:])
