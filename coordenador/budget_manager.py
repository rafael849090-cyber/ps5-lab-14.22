"""Controle financeiro local do laboratorio PS5 14.22.

Sem rede, HTTP, API keys ou execucao de comandos externos.
"""

import copy
import fcntl
import json
import os
import shutil
import tempfile
import time

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "documentacao/avaliacao_comparativa/registro_orcamento.json"

MONTHLY_LIMIT = Decimal("10.00")
SAFETY_MULTIPLIER = Decimal("2")
LOCK_TIMEOUT_SECONDS = 5

ACTIVE_STATES = {"reserved", "pending_verification"}

DEFAULT_PRICING = {
    "gpt-6.1-terra": {
        "input_per_million": "1",
        "output_per_million": "5",
        "currency": "USD",
        "status": "unverified",
        "source": "Registro documental anterior; reconfirmacao pendente",
    },
    "claude-sonnet-5-5": {
        "input_per_million": "2",
        "output_per_million": "10",
        "currency": "USD",
        "status": "unverified",
        "source": "Registro documental anterior; reconfirmacao pendente",
    },
}


class BudgetError(Exception):
    """Erro de validacao ou operacao financeira."""


def _money(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise BudgetError("Valor monetario invalido.")

    if not result.is_finite() or result < 0:
        raise BudgetError("Valor monetario deve ser finito e nao negativo.")

    return result.quantize(Decimal("0.000001"))


def _tokens(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BudgetError("Quantidade de tokens invalida.")
    return value


def _now():
    return datetime.now(timezone.utc)


def _timestamp():
    return _now().isoformat()


def _current_month():
    return _now().strftime("%Y-%m")


class BudgetManager:

    def __init__(self, data_path=DEFAULT_PATH, lock_timeout=LOCK_TIMEOUT_SECONDS):
        self.path = Path(data_path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.lock_timeout = lock_timeout

    def _default_data(self):
        return {
            "schema_version": 1,
            "currency": "USD",
            "monthly_limit": str(MONTHLY_LIMIT),
            "safety_multiplier": str(SAFETY_MULTIPLIER),
            "pricing": copy.deepcopy(DEFAULT_PRICING),
            "operations": [],
            "created_at": _timestamp(),
            "updated_at": _timestamp(),
        }

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)

        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False

        try:
            deadline = time.monotonic() + self.lock_timeout

            while time.monotonic() < deadline:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    time.sleep(0.1)

            if not acquired:
                raise BudgetError("Nao foi possivel obter o bloqueio financeiro.")

            yield

        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self):
        if not self.path.exists():
            return self._default_data()

        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise BudgetError(f"Registro financeiro invalido: {exc}")

        if not isinstance(data, dict):
            raise BudgetError("Estrutura principal do registro invalida.")

        if data.get("schema_version") != 1:
            raise BudgetError("Versao do registro financeiro nao suportada.")

        if data.get("currency") != "USD":
            raise BudgetError("Moeda do registro nao suportada.")

        if not isinstance(data.get("pricing"), dict):
            raise BudgetError("Catalogo de precos invalido.")

        if not isinstance(data.get("operations"), list):
            raise BudgetError("Lista de operacoes invalida.")

        _money(data.get("monthly_limit"))
        _money(data.get("safety_multiplier"))

        seen = set()
        for operation in data["operations"]:
            if not isinstance(operation, dict):
                raise BudgetError("Operacao invalida no registro.")

            operation_id = operation.get("id")
            if not isinstance(operation_id, str) or not operation_id:
                raise BudgetError("Operacao sem identificador valido.")

            if operation_id in seen:
                raise BudgetError("Identificador de operacao duplicado.")
            seen.add(operation_id)

            if operation.get("status") not in {
                "reserved", "pending_verification",
                "completed", "released"
            }:
                raise BudgetError("Estado de operacao desconhecido.")

            _money(operation.get("reserve_amount", "0"))

            if operation.get("status") == "completed":
                _money(operation.get("actual_cost"))

        return data

    def _atomic_write(self, data):
        serialized = json.dumps(
            data, ensure_ascii=False, indent=2
        ) + "\n"

        if self.path.exists():
            backup = self.path.with_name(
                self.path.name
                + "."
                + _now().strftime("%Y%m%dT%H%M%S%f")
                + ".bak"
            )
            shutil.copy2(self.path, backup)

            with backup.open("rb") as file:
                os.fsync(file.fileno())

        fd, temporary_name = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=self.path.parent,
        )

        temporary_path = Path(temporary_name)

        try:
            os.fchmod(fd, 0o600)

            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(serialized)
                file.flush()
                os.fsync(file.fileno())

            with temporary_path.open("r", encoding="utf-8") as file:
                checked = json.load(file)

            if not isinstance(checked, dict):
                raise BudgetError("Validacao do arquivo temporario falhou.")

            os.replace(temporary_path, self.path)

            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _pricing(self, data, model):
        pricing = data["pricing"].get(model)

        if not isinstance(pricing, dict):
            raise BudgetError("Modelo sem preco cadastrado.")

        if pricing.get("status") != "verified":
            raise BudgetError(
                "Preco nao verificado. Estimativa ou reserva bloqueada."
            )

        if pricing.get("currency") != "USD":
            raise BudgetError("Moeda do preco nao suportada.")

        return pricing

    def _estimate(self, data, model, input_tokens, output_tokens):
        input_tokens = _tokens(input_tokens)
        output_tokens = _tokens(output_tokens)
        pricing = self._pricing(data, model)

        input_price = _money(pricing["input_per_million"])
        output_price = _money(pricing["output_per_million"])

        cost = (
            Decimal(input_tokens) * input_price
            + Decimal(output_tokens) * output_price
        ) / Decimal(1_000_000)

        return cost.quantize(Decimal("0.000001"))

    def _status(self, data, month=None):
        month = month or _current_month()
        limit = _money(data["monthly_limit"])

        spent = _money(sum(
            (
                _money(op["actual_cost"])
                for op in data["operations"]
                if op["status"] == "completed"
                and op.get("charged_month") == month
            ),
            Decimal("0"),
        ))

        held = _money(sum(
            (
                _money(op["reserve_amount"])
                for op in data["operations"]
                if op["status"] in ACTIVE_STATES
            ),
            Decimal("0"),
        ))

        available = _money(
            max(Decimal("0"), limit - spent - held)
        )

        return {
            "month": month,
            "currency": "USD",
            "limit": str(limit),
            "spent": str(spent),
            "reserved": str(held),
            "available": str(available),
        }

    def get_budget_status(self):
        with self._locked():
            return self._status(self._load())

    def get_operation(self, operation_id):
        """Consulta uma operação existente sem modificar o registro."""
        if not isinstance(operation_id, str) or not operation_id:
            raise BudgetError("Identificador de operação inválido.")

        with self._locked():
            data = self._load()
            for operation in data["operations"]:
                if operation["id"] == operation_id:
                    return copy.deepcopy(operation)
            return None

    def estimate_cost(self, model, input_tokens, output_tokens):
        with self._locked():
            data = self._load()
            cost = self._estimate(
                data, model, input_tokens, output_tokens
            )
            return {
                "model": model,
                "estimated_cost": str(cost),
                "currency": "USD",
                "pricing_status": "verified",
            }

    def check_budget(self, estimated_cost):
        cost = _money(estimated_cost)
        required = _money(cost * SAFETY_MULTIPLIER)

        with self._locked():
            status = self._status(self._load())
            available = _money(status["available"])

            return {
                "approved": required <= available,
                "estimated_cost": str(cost),
                "required_reservation": str(required),
                "available": str(available),
                "currency": "USD",
            }

    def reserve_budget(self, operation):
        if not isinstance(operation, dict):
            raise BudgetError("Operacao deve ser um objeto.")

        operation_id = operation.get("id")
        model = operation.get("model")

        if not isinstance(operation_id, str) or not operation_id.strip():
            raise BudgetError("Identificador da operacao obrigatorio.")

        if not isinstance(model, str) or not model.strip():
            raise BudgetError("Modelo obrigatorio.")

        input_tokens = _tokens(operation.get("input_tokens"))
        output_tokens = _tokens(operation.get("output_tokens"))

        with self._locked():
            data = self._load()

            for existing in data["operations"]:
                if existing["id"] == operation_id:
                    raise BudgetError(
                        "ID de operacao ja utilizado. "
                        "Nao e permitido reutilizar reservas antigas."
                    )

            cost = self._estimate(
                data, model, input_tokens, output_tokens
            )
            reserve = _money(cost * _money(data["safety_multiplier"]))
            status = self._status(data)

            if reserve > _money(status["available"]):
                raise BudgetError("Saldo insuficiente para a reserva.")

            record = {
                "id": operation_id,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost": str(cost),
                "reserve_amount": str(reserve),
                "status": "reserved",
                "budget_month": _current_month(),
                "created_at": _timestamp(),
                "actual_cost": None,
                "charged_month": None,
                "evidence": None,
            }

            data["operations"].append(record)
            data["updated_at"] = _timestamp()
            self._atomic_write(data)

            return copy.deepcopy(record)

    def mark_pending_verification(self, operation_id, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise BudgetError("Motivo da pendencia obrigatorio.")

        with self._locked():
            data = self._load()
            operation = self._find(data, operation_id)

            if operation["status"] not in ACTIVE_STATES:
                raise BudgetError("Operacao nao possui reserva ativa.")

            operation["status"] = "pending_verification"
            operation["evidence"] = reason
            operation["updated_at"] = _timestamp()
            data["updated_at"] = _timestamp()
            self._atomic_write(data)

            return copy.deepcopy(operation)

    def record_actual_cost(self, operation_id, usage):
        if not isinstance(usage, dict):
            raise BudgetError("Dados de consumo invalidos.")

        with self._locked():
            data = self._load()
            operation = self._find(data, operation_id)

            if operation["status"] not in ACTIVE_STATES:
                raise BudgetError("Operacao nao possui reserva ativa.")

            if "actual_cost" in usage:
                actual = _money(usage["actual_cost"])
            else:
                actual = self._estimate(
                    data,
                    operation["model"],
                    _tokens(usage.get("input_tokens")),
                    _tokens(usage.get("output_tokens")),
                )

            charged_month = usage.get("charged_month", _current_month())

            if (
                not isinstance(charged_month, str)
                or len(charged_month) != 7
                or charged_month[4] != "-"
            ):
                raise BudgetError("Mes de cobranca invalido.")

            actual_input = None
            actual_output = None

            if "input_tokens" in usage:
                actual_input = _tokens(usage["input_tokens"])

            if "output_tokens" in usage:
                actual_output = _tokens(usage["output_tokens"])

            operation["status"] = "completed"
            operation["actual_cost"] = str(actual)
            operation["actual_input_tokens"] = actual_input
            operation["actual_output_tokens"] = actual_output
            operation["charged_month"] = charged_month
            operation["actual_recorded_at"] = _timestamp()
            operation["reserve_amount"] = "0.000000"
            data["updated_at"] = _timestamp()

            self._atomic_write(data)
            return copy.deepcopy(operation)

    def release_reservation(self, operation_id, evidence):
        if not isinstance(evidence, str) or not evidence.strip():
            raise BudgetError(
                "Evidencia obrigatoria para liberar a reserva."
            )

        with self._locked():
            data = self._load()
            operation = self._find(data, operation_id)

            if operation["status"] not in ACTIVE_STATES:
                raise BudgetError("Operacao nao possui reserva ativa.")

            operation["status"] = "released"
            operation["reserve_amount"] = "0.000000"
            operation["evidence"] = evidence
            operation["released_at"] = _timestamp()
            data["updated_at"] = _timestamp()

            self._atomic_write(data)
            return copy.deepcopy(operation)

    @staticmethod
    def _find(data, operation_id):
        for operation in data["operations"]:
            if operation["id"] == operation_id:
                return operation
        raise BudgetError("Operacao nao encontrada.")


_default_manager = BudgetManager()


def get_budget_status():
    return _default_manager.get_budget_status()


def estimate_cost(model, input_tokens, output_tokens):
    return _default_manager.estimate_cost(
        model, input_tokens, output_tokens
    )


def check_budget(estimated_cost):
    return _default_manager.check_budget(estimated_cost)


def reserve_budget(operation):
    return _default_manager.reserve_budget(operation)


def record_actual_cost(operation_id, usage):
    return _default_manager.record_actual_cost(operation_id, usage)


def release_reservation(operation_id, evidence):
    return _default_manager.release_reservation(operation_id, evidence)
