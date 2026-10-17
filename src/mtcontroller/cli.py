"""Administrační příkazy: `mtc-admin <příkaz>`.

  gen-master-key        vypíše nový master klíč (base64)
  create-superadmin E   založí superadmina (heslo se zadá interaktivně)
"""

from __future__ import annotations

import argparse
import getpass
import sys

from .security import SecretBox, hash_password


def _gen_master_key(_: argparse.Namespace) -> int:
    print(SecretBox.generate_master_key())
    return 0


def _create_superadmin(args: argparse.Namespace) -> int:
    from .db import operator_scope
    from .models import AppUser, AuditLog, RoleBinding, UserRole

    pw = getpass.getpass("Heslo (min. 12 znaků): ")
    if pw != getpass.getpass("Znovu: "):
        print("Hesla se neshodují", file=sys.stderr)
        return 1
    with operator_scope() as s:
        user = AppUser(email=args.email.lower(), password_hash=hash_password(pw))
        s.add(user)
        s.flush()
        s.add(RoleBinding(user_id=user.id, role=UserRole.superadmin))
        s.add(AuditLog(action="user.create_superadmin", target=f"user:{user.id}",
                       after={"email": user.email}))
    print(f"Superadmin {args.email} založen. MFA se nastaví při prvním přihlášení.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mtc-admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-master-key").set_defaults(fn=_gen_master_key)
    sa = sub.add_parser("create-superadmin")
    sa.add_argument("email")
    sa.set_defaults(fn=_create_superadmin)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
