
"""Editor local com backup, integridade e auditoria."""

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import uuid

from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

from coordenador.audit_logger import AuditLogger, AuditError


ROOT = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 1024 * 1024

ALLOWED_EXTENSIONS = {
    ".py", ".json", ".md", ".txt", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".html", ".css",
    ".js", ".sh", ".sql", ".xml",
}


class FileEditorError(Exception):
    pass


class FileEditor:
    def __init__(self, audit=None):
        self.audit = audit or AuditLogger()
        self._operations = {}

    def _resolve(self, relative_path):
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise FileEditorError("Caminho invalido.")

        candidate = Path(relative_path)

        if candidate.is_absolute() or ".." in candidate.parts:
            raise FileEditorError("Caminho externo ou traversal bloqueado.")

        current = ROOT

        for part in candidate.parts:
            if part in ("", "."):
                continue
            current = current / part
            if current.is_symlink():
                raise FileEditorError("Links simbolicos nao permitidos.")

        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(ROOT)
        except (OSError, ValueError):
            raise FileEditorError("Caminho inexistente ou externo.")

        if not resolved.is_file():
            raise FileEditorError("Destino precisa ser arquivo existente.")

        if resolved.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise FileEditorError("Extensao nao autorizada.")

        info = resolved.stat()

        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise FileEditorError("Tipo de arquivo ou hard link bloqueado.")

        if info.st_size > MAX_FILE_BYTES:
            raise FileEditorError("Arquivo excede 1 MiB.")

        return resolved

    @staticmethod
    def _hash(data):
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _read_bytes(path):
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        fd = os.open(str(path), flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise FileEditorError("Arquivo nao regular ou com hard links.")

            chunks = []
            total = 0

            while True:
                chunk = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise FileEditorError("Arquivo excede limite.")

            return b"".join(chunks)
        finally:
            os.close(fd)

    def _record(self, operation_id, event, details):
        self.audit.record(operation_id, event, details)

    @contextmanager
    def _locked(self):
        lock_path = ROOT / "coordenador" / ".file_editor.lock"

        if lock_path.is_symlink():
            raise FileEditorError("Arquivo de bloqueio inseguro.")

        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        fd = os.open(str(lock_path), flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise FileEditorError("Bloqueio nao e arquivo regular.")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _sync_directory(self, directory):
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _persist_state(self, operation_id, state):
        """Cria ou atualiza atomicamente o estado persistente da operacao."""
        if (
            not isinstance(operation_id, str)
            or len(operation_id) != 35
            or not operation_id.startswith("FE-")
            or any(c not in "0123456789abcdef" for c in operation_id[3:])
        ):
            raise FileEditorError("Identificador de operacao invalido.")

        if not isinstance(state, dict) or state.get("operation_id") != operation_id:
            raise FileEditorError("Estado nao corresponde a operacao.")

        directory = ROOT / "coordenador" / "file_editor_state"

        if directory.is_symlink():
            raise FileEditorError("Diretorio de estado inseguro.")

        directory.mkdir(mode=0o700, exist_ok=True)
        directory_stat = directory.lstat()

        if (
            not directory.is_dir()
            or directory.is_symlink()
            or directory_stat.st_mode & 0o077
        ):
            raise FileEditorError("Permissoes do diretorio de estado inseguras.")

        target = directory / f"{operation_id}.json"

        def canonical(value):
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")

        def validate_existing():
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(target), flags)
            try:
                info = os.fstat(fd)
                if not os.path.isfile(str(target)) or info.st_nlink != 1:
                    raise FileEditorError("Registro persistente inseguro.")
                if info.st_size > 65536:
                    raise FileEditorError("Registro persistente excessivamente grande.")

                chunks = []
                remaining = 65537
                while remaining:
                    chunk = os.read(fd, min(8192, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)

                raw = b"".join(chunks)
                if len(raw) > 65536:
                    raise FileEditorError("Registro persistente excessivamente grande.")
            finally:
                os.close(fd)

            try:
                previous = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise FileEditorError("Registro persistente invalido.") from exc

            if (
                not isinstance(previous, dict)
                or previous.get("operation_id") != operation_id
            ):
                raise FileEditorError("Registro pertence a outra operacao.")

            stored_hash = previous.pop("_state_sha256", None)
            if not isinstance(stored_hash, str):
                raise FileEditorError("Registro antigo sem hash; revisao manual.")

            actual_hash = hashlib.sha256(canonical(previous)).hexdigest()
            if actual_hash != stored_hash:
                raise FileEditorError("Integridade do estado persistente comprometida.")

        try:
            target.lstat()
            exists = True
        except FileNotFoundError:
            exists = False

        if exists:
            validate_existing()

        clean_state = dict(state)
        clean_state.pop("_state_sha256", None)
        state_hash = hashlib.sha256(canonical(clean_state)).hexdigest()

        record = dict(clean_state)
        record["_state_sha256"] = state_hash
        data = canonical(record)

        fd, temp_name = tempfile.mkstemp(
            prefix=".state_",
            suffix=".tmp",
            dir=str(directory),
        )

        try:
            with os.fdopen(fd, "wb") as temp:
                temp.write(data)
                temp.flush()
                os.fchmod(temp.fileno(), 0o600)
                os.fsync(temp.fileno())

            if exists:
                os.replace(temp_name, target)
            else:
                os.link(temp_name, target)

            self._sync_directory(directory)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def read_for_edit(self, relative_path):
        path = self._resolve(relative_path)
        data = self._read_bytes(path)

        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            raise FileEditorError("Arquivo nao e UTF-8 valido.")

        self._record(
            "FE-" + uuid.uuid4().hex,
            "file_editor_read",
            {
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": self._hash(data),
                "bytes": len(data),
            },
        )
        return content

    def preview_change(self, relative_path, new_content):
        path = self._resolve(relative_path)

        if not isinstance(new_content, str):
            raise FileEditorError("Conteudo deve ser texto.")

        new_data = new_content.encode("utf-8")
        if len(new_data) > MAX_FILE_BYTES:
            raise FileEditorError("Novo conteudo excede 1 MiB.")

        old_data = self._read_bytes(path)

        return {
            "path": path.relative_to(ROOT).as_posix(),
            "old_sha256": self._hash(old_data),
            "new_sha256": self._hash(new_data),
            "old_bytes": len(old_data),
            "new_bytes": len(new_data),
            "will_create_backup": True,
            "will_write": False,
        }

    def _create_backup(self, path, original):
        original_hash = self._hash(original)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

        backup = path.with_name(
            f".{path.name}.{stamp}.{uuid.uuid4().hex}.bak"
        )

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(str(backup), flags, 0o600)

        try:
            view = memoryview(original)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise FileEditorError("Falha ao gravar backup.")
                view = view[written:]

            os.fchmod(fd, stat.S_IMODE(path.stat().st_mode))
            os.fsync(fd)
        finally:
            os.close(fd)

        self._sync_directory(path.parent)

        if self._hash(self._read_bytes(backup)) != original_hash:
            raise FileEditorError("Integridade do backup nao confirmada.")

        return {
            "backup": backup.relative_to(ROOT).as_posix(),
            "sha256": original_hash,
        }

    def backup_file(self, relative_path):
        with self._locked():
            path = self._resolve(relative_path)
            original = self._read_bytes(path)
            return self._create_backup(path, original)

    def write_file(self, relative_path, new_content):
        if not isinstance(new_content, str):
            raise FileEditorError("Conteudo deve ser texto.")

        new_data = new_content.encode("utf-8")
        if len(new_data) > MAX_FILE_BYTES:
            raise FileEditorError("Novo conteudo excede 1 MiB.")

        operation_id = "FE-" + uuid.uuid4().hex
        temp_name = None
        replaced = False

        with self._locked():
            path = self._resolve(relative_path)
            original_stat = path.stat()
            old_data = self._read_bytes(path)
            old_hash = self._hash(old_data)
            new_hash = self._hash(new_data)
            relative = path.relative_to(ROOT).as_posix()

            self._operations[operation_id] = {"status": "started"}

            self._record(
                operation_id,
                "file_edit_started",
                {
                    "path": relative,
                    "old_sha256": old_hash,
                    "new_sha256": new_hash,
                },
            )

            backup_info = self._create_backup(path, old_data)

            self._record(
                operation_id,
                "file_backup_created",
                {
                    "path": relative,
                    "backup": backup_info["backup"],
                    "sha256": backup_info["sha256"],
                },
            )

            state = {
                "operation_id": operation_id,
                "status": "backup_confirmed",
                "path": relative,
                "backup": backup_info["backup"],
                "old_sha256": old_hash,
                "new_sha256": new_hash,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._persist_state(operation_id, state)

            fd, temp_name = tempfile.mkstemp(
                prefix=".file_editor_",
                suffix=".tmp",
                dir=str(path.parent),
            )

            try:
                with os.fdopen(fd, "wb") as temp:
                    temp.write(new_data)
                    temp.flush()
                    os.fsync(temp.fileno())

                os.chmod(temp_name, stat.S_IMODE(original_stat.st_mode))

                current_stat = path.stat()
                current_data = self._read_bytes(path)

                if (
                    self._hash(current_data) != old_hash
                    or current_stat.st_ino != original_stat.st_ino
                    or current_stat.st_dev != original_stat.st_dev
                ):
                    raise FileEditorError("Arquivo mudou durante a operacao.")

                if path.is_symlink():
                    raise FileEditorError("Destino virou link simbolico.")

                self._record(
                    operation_id,
                    "file_edit_ready_to_replace",
                    {
                        "path": relative,
                        "backup": backup_info["backup"],
                        "old_sha256": old_hash,
                        "new_sha256": new_hash,
                    },
                )

                state["status"] = "replacement_started"
                self._persist_state(operation_id, state)

                os.replace(temp_name, path)
                temp_name = None
                replaced = True

                self._sync_directory(path.parent)

                final_hash = self._hash(self._read_bytes(path))
                if final_hash != new_hash:
                    state["status"] = "integrity_error"
                    self._persist_state(operation_id, state)
                    raise FileEditorError("Hash final incorreto.")

                state["status"] = "changed_audit_pending"
                self._persist_state(operation_id, state)

                try:
                    self._record(
                        operation_id,
                        "file_edit_completed",
                        {
                            "path": relative,
                            "backup": backup_info["backup"],
                            "old_sha256": old_hash,
                            "new_sha256": final_hash,
                            "timestamp_utc": datetime.now(
                                timezone.utc
                            ).isoformat(),
                        },
                    )
                except Exception as exc:
                    state["status"] = "changed_audit_failed"
                    self._persist_state(operation_id, state)
                    raise FileEditorError(
                        "Arquivo alterado; auditoria final falhou. "
                        "Revisao manual obrigatoria."
                    ) from exc

                state["status"] = "completed"
                self._persist_state(operation_id, state)
                self._operations[operation_id] = {"status": "completed"}

                return {
                    "operation_id": operation_id,
                    "status": "completed",
                    "path": relative,
                    "backup": backup_info["backup"],
                    "old_sha256": old_hash,
                    "new_sha256": final_hash,
                }

            except Exception:
                if not replaced:
                    self._operations[operation_id] = {
                        "status": "failed_before_replace"
                    }
                else:
                    self._operations[operation_id] = {
                        "status": "changed_review_required"
                    }
                raise

            finally:
                if temp_name and os.path.exists(temp_name):
                    os.unlink(temp_name)

    def get_operation_status(self, operation_id):
        if (
            not isinstance(operation_id, str)
            or len(operation_id) != 35
            or not operation_id.startswith("FE-")
            or any(c not in "0123456789abcdef" for c in operation_id[3:])
        ):
            return {"status": "unknown"}

        if operation_id in self._operations:
            return dict(self._operations[operation_id])

        directory = ROOT / "coordenador" / "file_editor_state"
        if directory.is_symlink() or not directory.is_dir():
            return {"status": "recovery_required"}

        try:
            directory_stat = directory.lstat()
            if directory_stat.st_mode & 0o077:
                return {"status": "recovery_required"}

            state_path = directory / f"{operation_id}.json"
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(state_path), flags)

            try:
                info = os.fstat(fd)
                if not os.path.isfile(str(state_path)) or info.st_nlink != 1:
                    return {"status": "recovery_required"}
                if info.st_size > 65536:
                    return {"status": "recovery_required"}

                chunks = []
                while True:
                    chunk = os.read(fd, 8192)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if sum(map(len, chunks)) > 65536:
                        return {"status": "recovery_required"}
            finally:
                os.close(fd)

            state = json.loads(b"".join(chunks).decode("utf-8"))
            if not isinstance(state, dict) or state.get("operation_id") != operation_id:
                return {"status": "recovery_required"}

            stored_hash = state.pop("_state_sha256", None)
            canonical = json.dumps(
                state,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")

            actual_hash = hashlib.sha256(canonical).hexdigest()
            if not isinstance(stored_hash, str) or stored_hash != actual_hash:
                return {"status": "recovery_required"}

            return state

        except FileNotFoundError:
            return {"status": "unknown"}
        except (OSError, ValueError, UnicodeDecodeError, TypeError):
            return {"status": "recovery_required"}

