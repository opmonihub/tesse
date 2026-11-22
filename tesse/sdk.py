"""Somente interfaces públicas do SDK global; nenhum segredo em logs/YAML."""

import hashlib
import os
import secrets
from pathlib import Path

from ebinex_sdk import (
    AuthPersistenceConfig,
    ClientConfig,
    EbinexClient,
    OrderJournalConfig,
    ProtectedStorageConfig,
)
from ebinex_sdk.storage import ProtectedStore, protect_private_path


def storage_config(root: Path, *, create=False):
    private = root / "private"
    vault = private / "sdk"
    key = private / "sdk.key"
    if create:
        if not private.exists():
            private.mkdir(parents=True)
            protect_private_path(private, directory=True)
        if not key.exists():
            if vault.exists() and any(vault.iterdir()):
                raise ValueError(
                    "Chave ausente com dados existentes: restaure a chave original; não recriar"
                )
            with key.open("xb") as out:
                out.write(secrets.token_bytes(32))
                out.flush()
                os.fsync(out.fileno())
            protect_private_path(key, directory=False)
    if not key.exists():
        raise ValueError("Armazenamento não inicializado; execute init")
    if key.stat().st_size != 32:
        raise ValueError("Chave inválida: restaure backup original sem apagar o journal")
    return ProtectedStorageConfig(data_directory=vault, key_file=key)


def make_http_transport(config):
    from ebinex_sdk.transports.http import HttpxTransport

    return HttpxTransport(
        config, allowed_origins=(str(config.sockjs_base_url),)
    )


def make_client(root, *, orders=False, clock=None):
    storage = storage_config(root)
    config = ClientConfig(
        environment="TEST",
        enable_test_orders=orders,
        auth_persistence=AuthPersistenceConfig(enabled=True, storage=storage),
        order_journal=OrderJournalConfig(storage=storage),
    )
    if clock is None:
        return EbinexClient(config=config)
    from ebinex_sdk.transports.websocket import WebsocketsTransport

    from .broker_time import ObservedWebSocket

    http = make_http_transport(config)
    return EbinexClient(
        config=config,
        http=http,
        websocket_factory=lambda: ObservedWebSocket(WebsocketsTransport(config, http), clock),
    )


async def journal_snapshot(root, intent_id=None):
    """Executar com o cliente fechado: o cofre tem posse exclusiva por processo."""
    protected = ProtectedStore(storage_config(root))
    try:
        await protected.open()
        stored = await protected.read_stored_session(allow_expired=True)
        if not stored:
            raise ValueError("Sessão ausente/expirada; execute login no terminal")
        binding = stored.binding
        digest = hashlib.sha256(f"{binding.identity}|{binding.account_id}".encode()).hexdigest()
        entry = await protected.read_journal(binding, intent_id) if intent_id else None
        return digest, entry
    finally:
        await protected.close()
