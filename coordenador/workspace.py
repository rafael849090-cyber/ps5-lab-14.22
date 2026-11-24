"""Acesso local, restrito e somente de leitura ao workspace."""

from pathlib import Path
from uuid import uuid4

from coordenador.audit_logger import AuditLogger


ROOT = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 1024 * 1024

ALLOWED_EXTENSIONS = {
    ".py", ".json", ".md", ".txt", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".html", ".css",
    ".js", ".sh", ".sql", ".xml"
}


class WorkspaceError(Exception):
    pass


class Workspace:
    def __init__(self, audit=None):
        self.root = ROOT.resolve()
        self.audit = audit or AuditLogger()

    def _resolve(self, relative_path="."):
        if not isinstance(relative_path, str) or not relative_path:
            raise WorkspaceError("Caminho inválido.")

        requested = Path(relative_path)

        if requested.is_absolute() or ".." in requested.parts:
            raise WorkspaceError("Caminho fora das regras permitidas.")

        candidate = self.root / requested
        current = self.root

        for part in requested.parts:
            if part == ".":
                continue
            current = current / part
            if current.is_symlink():
                raise WorkspaceError("Link simbólico bloqueado.")

        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise WorkspaceError("Caminho inexistente ou não autorizado.") from exc

        return resolved

    def _audit(self, event, details):
        operation_id = "WS-" + uuid4().hex
        self.audit.record(operation_id, event, details)

    def list_directory(self, relative_path="."):
        directory = self._resolve(relative_path)

        if not directory.is_dir():
            raise WorkspaceError("O caminho não é um diretório.")

        entries = []

        for item in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if item.is_symlink():
                continue

            if item.is_dir():
                kind = "directory"
            elif item.is_file():
                kind = "file"
            else:
                continue

            entries.append({"name": item.name, "type": kind})

        self._audit("workspace_listed", {
            "path": str(directory.relative_to(self.root)),
            "entries": len(entries),
        })

        return entries

    def read_file(self, relative_path):
        file_path = self._resolve(relative_path)

        if not file_path.is_file():
            raise WorkspaceError("O caminho não é um arquivo.")

        if file_path.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise WorkspaceError("Extensão não autorizada.")

        size = file_path.stat().st_size

        if size > MAX_FILE_BYTES:
            raise WorkspaceError("Arquivo excede 1 MiB.")

        try:
            content = file_path.read_bytes()

            if len(content) > MAX_FILE_BYTES or b"\x00" in content:
                raise WorkspaceError("Arquivo binário ou acima do limite.")

            text = content.decode("utf-8")

        except UnicodeDecodeError as exc:
            raise WorkspaceError("Arquivo não está em UTF-8 válido.") from exc

        self._audit("workspace_file_read", {
            "path": str(file_path.relative_to(self.root)),
            "bytes": len(content),
        })

        return text

    def analyze_file(self, relative_path, terms=None):
        content = self.read_file(relative_path)
        lines = content.splitlines()

        result = {
            "path": relative_path,
            "characters": len(content),
            "lines": len(lines),
            "words": len(content.split()),
        }

        if terms is not None:
            if not isinstance(terms, (list, tuple)):
                raise WorkspaceError("Termos devem ser uma lista.")

            if len(terms) > 20 or any(
                not isinstance(term, str) or not term or len(term) > 100
                for term in terms
            ):
                raise WorkspaceError("Lista de termos inválida.")

            result["term_counts"] = {
                term: content.casefold().count(term.casefold())
                for term in terms
            }

        self._audit("workspace_file_analyzed", {
            "path": relative_path,
            "lines": result["lines"],
            "characters": result["characters"],
        })

        return result
