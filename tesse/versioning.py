"""SQLite é a autoridade; YAML é uma projeção recuperável com detecção de conflito."""

import json

import yaml

from .config import Strategy, atomic_write, load_config


def recover_projection(store, path):
    pending = store.get("projection")
    if not pending:
        return
    config = load_config(path)
    target = Strategy(**pending["params"])
    if config.fingerprint() != pending["expected"] and config.strategy != target:
        raise ValueError(
            "Conflito YAML durante promoção; preserve o arquivo e confira versions/report"
        )
    config.strategy = target
    atomic_write(path, yaml.safe_dump(config.model_dump(), sort_keys=False, allow_unicode=True))
    with store.transaction():
        store.set("config_fingerprint", config.fingerprint())
        store.set("projection", None)


def project_active(store, path, expected=None):
    config = load_config(path)
    if expected is not None and config.fingerprint() != expected:
        raise ValueError("YAML mudou: projeção cancelada para preservar edição manual")
    store.set(
        "projection",
        dict(params=json.loads(store.active_version()["params"]), expected=config.fingerprint()),
    )
    recover_projection(store, path)


def sync_manual(store, config):
    active = Strategy(**json.loads(store.active_version()["params"]))
    if config.strategy != active:
        if store.open_session() or store.pending():
            return False
        identity = store.add_version(config.strategy, "manual_experimental")
        store.promote(identity, cancel_pilot=True, experimental=True)
        store.event("manual_profile_applied", version=identity)
    store.set("config_fingerprint", config.fingerprint())
    return True
