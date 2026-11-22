"""Local credentials; never export secrets or evaluate shell expressions."""

import os
import shlex
from pathlib import Path

from ebinex_sdk import Credentials


def credentials_from_env(path: Path) -> Credentials | None:
    names = ("EBINEX_EMAIL", "EBINEX_PASSWORD")
    values = {name: os.environ[name] for name in names if name in os.environ}
    if not values and path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.removeprefix("export ").split("=", 1)
            name, value = name.strip(), value.strip()
            if name not in names:
                continue
            if value.startswith(('"', "'")):
                try:
                    parts = shlex.split(value, comments=True)
                except ValueError:
                    raise ValueError("Aspas inválidas no arquivo .env") from None
                if len(parts) != 1:
                    raise ValueError("Valor inválido no arquivo .env")
                value = parts[0]
            else:
                value = value.split(" #", 1)[0].rstrip()
            values[name] = value
    if not values:
        return None
    if not all(values.get(name) for name in names):
        raise ValueError("Configure EBINEX_EMAIL e EBINEX_PASSWORD juntos")
    return Credentials(email=values[names[0]], password=values[names[1]])
