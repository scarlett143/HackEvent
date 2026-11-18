"""Create the first Admin on a fresh install (Runbook B5, HE-132).

    docker compose exec api python -m scripts.create_admin --email admin@example.org --name "Admin"

Everyone else joins by invite (doc 07 §2), and an invite needs an Admin to send it, so the
first Admin is made here. The password is read from the terminal (never an argument, so
it stays out of shell history and `ps`), twice, and must pass the same policy as invite
acceptance. Refuses once any Admin exists: further Admins are invited from Admin → Users.
TOTP 2FA is enrolled at the first sign-in (/admin/2fa), as for every Admin.
"""

import argparse
import asyncio
import getpass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from api.auth import check_password_policy, hash_password
from api.errors import ApiError
from common.config import get_settings
from common.models import User, UserRole


async def create(database_url: str, email: str, name: str, password: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with AsyncSession(engine) as db, db.begin():
            admins = await db.scalar(
                select(func.count()).select_from(User).where(User.role == UserRole.admin)
            )
            if admins:
                raise SystemExit(
                    "REFUSED: an Admin already exists. Invite more from Admin → Users."
                )
            if await db.scalar(select(User.id).where(User.email == email)) is not None:
                raise SystemExit(f"REFUSED: a user with {email} already exists.")
            db.add(
                User(
                    email=email,
                    name=name,
                    role=UserRole.admin,
                    password_hash=hash_password(password),
                )
            )
    finally:
        await engine.dispose()


def main() -> None:
    p = argparse.ArgumentParser(description="Create the first Admin.")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    args = p.parse_args()
    email, name = args.email.strip().lower(), args.name.strip()
    if "@" not in email or not name:
        raise SystemExit("Give a valid --email and a non-empty --name.")
    password = getpass.getpass("Password (12+ characters): ")
    try:
        check_password_policy(password)
    except ApiError as exc:
        raise SystemExit(f"Weak password: {exc.message}") from None
    if getpass.getpass("Repeat password: ") != password:
        raise SystemExit("Passwords differ.")
    asyncio.run(create(get_settings().database_url, email, name, password))
    print(f"Admin {email} created. Sign in, then set up 2FA at /admin/2fa.")


if __name__ == "__main__":
    main()
