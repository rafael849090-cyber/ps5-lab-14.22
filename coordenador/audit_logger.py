"""Auditoria local das operações do coordenador."""

import fcntl
import json
import os
import re
import tempfile

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "documentacao/avaliacao_comparativa/auditoria_integracao.jsonl"
MAX_EVENT_BYTES = 16384

FORBIDDEN_KEYS = {
    "authorization",
    "api_key",
    "openai_api_key",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "payload",
    "prompt",
}


class AuditError(Exception):
    """Erro de validação ou gravação da auditoria."""


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _sanitize(value, depth=0):
    if depth > 8:
        raise AuditError("Profundidade excessiva nos metadados.")

    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditError("Chave de metadado inválida.")
            if key.lower() in FORBIDDEN_KEYS:
                raise AuditError("Campo sensível proibido na auditoria.")
            result[key] = _sanitize(item, depth + 1)
        return result

    if isinstance(value, list):
        if len(value) > 100:
            raise AuditError("Lista de metadados excessivamente longa.")
        return [_sanitize(item, depth + 1) for item in value]

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    raise AuditError("Tipo de metadado não permitido.")


class AuditLogger:
    def __init__(self, path=DEFAULT_PATH, lock_timeout=5):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.lock_timeout = lock_timeout

        resolved_root = ROOT.resolve()
        resolved_parent = self.path.parent.resolve()

        if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
            raise AuditError("Caminho de auditoria fora do projeto.")

    def _validate_existing(self):
        if not self.path.exists():
            return

        if self.path.is_symlink():
            raise AuditError("Arquivo de auditoria não pode ser link simbólico.")

        try:
            with self.path.open("rb") as file:
                for line_number, line in enumerate(file, 1):
                    if not line.endswith(b"\n"):
                        raise AuditError(
                            f"Registro incompleto na linha {line_number}."
                        )

                    event = json.loads(line.decode("utf-8"))

                    if not isinstance(event, dict):
                        raise AuditError("Evento de auditoria inválido.")

                    if not isinstance(event.get("event_id"), str):
                        raise AuditError("Evento sem identificador.")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuditError("Arquivo de auditoria inválido.") from exc

    def record(self, operation_id, event_type, details=None):
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise AuditError("Identificador da operação obrigatório.")

        if not isinstance(event_type, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{1,63}", event_type
        ):
            raise AuditError("Tipo de evento inválido.")

        if details is None:
            details = {}

        if not isinstance(details, dict):
            raise AuditError("Detalhes devem ser um objeto.")

        event = {
            "event_id": str(uuid4()),
            "operation_id": operation_id,
            "timestamp": _timestamp(),
            "event_type": event_type,
            "details": _sanitize(details),
        }

        serialized = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")

        if len(serialized) > MAX_EVENT_BYTES:
            raise AuditError("Evento excede o limite permitido.")

        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise AuditError("Links simbólicos não são permitidos.")

        lock_fd = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )

        acquired = False

        try:
            import time

            deadline = time.monotonic() + self.lock_timeout

            while time.monotonic() < deadline:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    time.sleep(0.1)

            if not acquired:
                raise AuditError("Não foi possível obter bloqueio da auditoria.")

            self._validate_existing()

            fd = os.open(
                self.path,
                os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
            )

            try:
                os.fchmod(fd, 0o600)
                remaining = memoryview(serialized)

                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise AuditError("Gravação incompleta da auditoria.")
                    remaining = remaining[written:]

                os.fsync(fd)
            finally:
                os.close(fd)

            return event

        except AuditError:
            raise
        except OSError as exc:
            raise AuditError("Falha ao gravar auditoria.") from exc
        finally:
            if acquired:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
