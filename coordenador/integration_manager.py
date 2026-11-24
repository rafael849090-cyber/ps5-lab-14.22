"""Coordenador local de orçamento, aprovação e auditoria.

Esta versão não realiza chamadas HTTP, não lê arquivos de projeto
e não acessa credenciais.
"""

import hashlib
import json
import re
import threading
from uuid import uuid4

from coordenador.approval_manager import (
    ApprovalManager,
    ApprovalError,
    EXPECTED_ENDPOINT,
)
from coordenador.budget_manager import BudgetError
from coordenador.audit_logger import AuditError


class IntegrationError(Exception):
    """Erro no fluxo local de integração."""


class IntegrationManager:
    def __init__(self, budget_manager, approval_manager, audit_logger):
        if budget_manager is None or approval_manager is None or audit_logger is None:
            raise IntegrationError("Todos os gerenciadores são obrigatórios.")

        if not isinstance(approval_manager, ApprovalManager):
            raise IntegrationError("ApprovalManager inválido.")

        self.budget = budget_manager
        self.approval = approval_manager
        self.audit = audit_logger
        self._operations = {}
        self._lock = threading.RLock()

    @staticmethod
    def _validate_operation(operation):
        if not isinstance(operation, dict):
            raise IntegrationError("Operação deve ser um objeto.")

        operation_id = operation.get("id")
        model = operation.get("model")
        input_tokens = operation.get("input_tokens")
        output_tokens = operation.get("output_tokens")
        payload = operation.get("payload")
        purpose = operation.get("purpose")

        if not isinstance(operation_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{8,80}", operation_id
        ):
            raise IntegrationError("ID inválido. Use 8 a 80 caracteres seguros.")

        if not isinstance(model, str) or not model.strip():
            raise IntegrationError("Modelo obrigatório.")

        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
            raise IntegrationError("Estimativa de entrada inválida.")

        if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens < 0:
            raise IntegrationError("Estimativa de saída inválida.")

        if not isinstance(payload, dict):
            raise IntegrationError("Payload inválido.")

        if payload.get("model") != model:
            raise IntegrationError("Modelo do payload diverge da operação.")

        if not isinstance(purpose, str) or not purpose.strip():
            raise IntegrationError("Finalidade obrigatória.")

        if len(purpose) > 500:
            raise IntegrationError("Finalidade excede o limite.")

        try:
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise IntegrationError("Payload não serializável.") from exc

        if len(serialized) > 48 * 1024:
            raise IntegrationError("Payload excede 48 KiB.")

        return {
            "id": operation_id,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "payload": payload,
            "purpose": purpose,
            "payload_sha256": hashlib.sha256(serialized).hexdigest(),
        }

    def _event(self, operation_id, event_type, details=None):
        try:
            self.audit.record(operation_id, event_type, details or {})
        except AuditError as exc:
            raise IntegrationError(
                "Falha na auditoria. Operação bloqueada."
            ) from exc

    def prepare_operation(self, operation):
        data = self._validate_operation(operation)

        with self._lock:
            if data["id"] in self._operations:
                raise IntegrationError("ID já utilizado nesta sessão.")

            # Registrar a intenção antes de criar a reserva.
            self._operations[data["id"]] = {
                "status": "preparing",
                "payload_sha256": data["payload_sha256"],
                "model": data["model"],
            }

            try:
                self._event(
                    data["id"],
                    "operation_prepared",
                    {
                        "model": data["model"],
                        "payload_sha256": data["payload_sha256"],
                    },
                )

                estimate = self.budget.estimate_cost(
                    data["model"],
                    data["input_tokens"],
                    data["output_tokens"],
                )

                if estimate.get("pricing_status") != "verified":
                    raise IntegrationError("Preço não verificado.")

                reservation = self.budget.reserve_budget({
                    "id": data["id"],
                    "model": data["model"],
                    "input_tokens": data["input_tokens"],
                    "output_tokens": data["output_tokens"],
                })

                # Nunca tratar uma reserva anterior como uma operação nova.
                if reservation.get("status") != "reserved":
                    raise IntegrationError("Reserva não está em estado novo.")

                if reservation.get("id") != data["id"]:
                    raise IntegrationError("ID da reserva divergente.")

                if reservation.get("model") != data["model"]:
                    raise IntegrationError("Modelo da reserva divergente.")

                self._operations[data["id"]].update({
                    "status": "reserved",
                    "estimated_cost": reservation["estimated_cost"],
                    "reserve_amount": reservation["reserve_amount"],
                    "payload": data["payload"],
                    "purpose": data["purpose"],
                    "input_tokens": data["input_tokens"],
                    "output_tokens": data["output_tokens"],
                })

                self._event(
                    data["id"],
                    "budget_reserved",
                    {
                        "estimated_cost": reservation["estimated_cost"],
                        "reserve_amount": reservation["reserve_amount"],
                        "currency": "USD",
                    },
                )

                return {
                    "operation_id": data["id"],
                    "status": "reserved",
                    "model": data["model"],
                    "estimated_cost": reservation["estimated_cost"],
                    "reserve_amount": reservation["reserve_amount"],
                    "payload_sha256": data["payload_sha256"],
                }

            except Exception as exc:
                self._operations[data["id"]]["status"] = "blocked"
                self._operations[data["id"]]["reason"] = type(exc).__name__

                try:
                    self._event(
                        data["id"],
                        "operation_blocked",
                        {"reason": type(exc).__name__},
                    )
                except IntegrationError:
                    pass

                raise

    def request_approval(self, operation_id):
        with self._lock:
            operation = self._operations.get(operation_id)

            if not operation or operation.get("status") != "reserved":
                raise IntegrationError("Operação não está pronta para aprovação.")

            try:
                authorization = self.approval.request_approval(
                    operation_id=operation_id,
                    payload=operation["payload"],
                    endpoint=EXPECTED_ENDPOINT,
                    purpose=operation["purpose"],
                    estimated_input_tokens=operation["input_tokens"],
                    estimated_output_tokens=operation["output_tokens"],
                    estimated_cost=operation["estimated_cost"],
                    reserve_amount=operation["reserve_amount"],
                )

                if authorization.operation_id != operation_id:
                    raise IntegrationError("Autorização vinculada a outro ID.")

                if authorization.payload_sha256 != operation["payload_sha256"]:
                    raise IntegrationError("Hash da autorização divergente.")

                operation["status"] = "approved"
                operation["authorization"] = authorization

                self._event(
                    operation_id,
                    "human_approved",
                    {
                        "authorization_id": authorization.authorization_id,
                        "payload_sha256": authorization.payload_sha256,
                    },
                )

                return authorization

            except (ApprovalError, IntegrationError) as exc:
                operation["status"] = "approval_failed"

                # A negativa/interrupção ocorreu antes de qualquer transmissão.
                try:
                    self.budget.release_reservation(
                        operation_id,
                        "Aprovação não concedida; transmissão não iniciada.",
                    )
                    operation["status"] = "released"
                    self._event(operation_id, "reservation_released", {
                        "reason": "approval_not_granted"
                    })
                except (BudgetError, AuditError, IntegrationError):
                    operation["status"] = "pending_verification"
                    try:
                        self.budget.mark_pending_verification(
                            operation_id,
                            "Falha ao concluir cancelamento ou auditoria.",
                        )
                    except BudgetError:
                        pass

                raise IntegrationError(
                    "Aprovação não concluída; operação bloqueada."
                ) from exc

            except Exception as exc:
                operation["status"] = "pending_verification"
                try:
                    self.budget.mark_pending_verification(
                        operation_id,
                        "Resultado da aprovação não pôde ser confirmado.",
                    )
                except BudgetError:
                    pass
                raise IntegrationError(
                    "Estado da aprovação incerto; reserva preservada."
                ) from exc

    def consume_authorization(self, operation_id):
        with self._lock:
            operation = self._operations.get(operation_id)

            if not operation or operation.get("status") != "approved":
                raise IntegrationError(
                    "Operacao nao esta aprovada para consumo."
                )

            authorization = operation.get("authorization")
            if authorization is None:
                raise IntegrationError("Autorizacao ausente.")

            self._event(
                operation_id,
                "authorization_consumption_requested",
                {
                    "authorization_id": authorization.authorization_id,
                    "payload_sha256": operation["payload_sha256"],
                },
            )

            try:
                serialized = self.approval.consume(
                    authorization=authorization,
                    operation_id=operation_id,
                    payload=operation["payload"],
                    endpoint=EXPECTED_ENDPOINT,
                    purpose=operation["purpose"],
                )
            except ApprovalError as exc:
                operation["status"] = "blocked"
                self._event(
                    operation_id,
                    "authorization_consumption_blocked",
                    {"reason": type(exc).__name__},
                )
                raise IntegrationError(
                    "Consumo da autorizacao bloqueado."
                ) from exc

            operation["status"] = "authorization_consumed"

            try:
                self._event(
                    operation_id,
                    "authorization_consumed",
                    {
                        "authorization_id": authorization.authorization_id,
                        "payload_sha256": operation["payload_sha256"],
                    },
                )
            except IntegrationError:
                operation["status"] = "pending_verification"
                try:
                    self.budget.mark_pending_verification(
                        operation_id,
                        "Autorizacao consumida, mas auditoria falhou antes da entrega dos bytes.",
                    )
                except BudgetError:
                    pass
                raise

            return serialized

    def mark_transmission_started(self, operation_id):
        with self._lock:
            operation = self._operations.get(operation_id)

            if not operation or operation.get("status") != "authorization_consumed":
                raise IntegrationError(
                    "Operacao nao esta autorizada para iniciar transmissao."
                )

            self._event(
                operation_id,
                "transmission_started",
                {
                    "payload_sha256": operation["payload_sha256"],
                    "destination": EXPECTED_ENDPOINT,
                },
            )

            operation["status"] = "transmission_started"
            return {"operation_id": operation_id, "status": "transmission_started"}

    def record_success(self, operation_id, usage):
        with self._lock:
            operation = self._operations.get(operation_id)

            if not operation or operation.get("status") != "transmission_started":
                raise IntegrationError(
                    "Operacao nao esta aguardando registro de resultado."
                )

            if not isinstance(usage, dict):
                raise IntegrationError("Dados de uso invalidos.")

            self._event(
                operation_id,
                "provider_usage_received",
                {
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                },
            )

            try:
                recorded = self.budget.record_actual_cost(operation_id, usage)
            except BudgetError as exc:
                operation["status"] = "pending_verification"
                try:
                    self.budget.mark_pending_verification(
                        operation_id,
                        "Resposta recebida, mas o custo real nao pode ser registrado.",
                    )
                except BudgetError:
                    pass
                raise IntegrationError(
                    "Resultado recebido; verificacao financeira pendente."
                ) from exc

            # O custo ja foi persistido. Nunca repetir o registro
            # automaticamente se a auditoria final falhar.
            operation["status"] = "completed_audit_pending"

            try:
                self._event(
                    operation_id,
                    "operation_completed",
                    {
                        "actual_cost": recorded["actual_cost"],
                        "actual_input_tokens": recorded.get("actual_input_tokens"),
                        "actual_output_tokens": recorded.get("actual_output_tokens"),
                        "charged_month": recorded["charged_month"],
                    },
                )
            except (AuditError, IntegrationError):
                raise IntegrationError(
                    "Custo registrado; auditoria final pendente."
                )

            operation["status"] = "completed"

            return {
                "operation_id": operation_id,
                "status": "completed",
                "actual_cost": recorded["actual_cost"],
                "actual_input_tokens": recorded.get("actual_input_tokens"),
                "actual_output_tokens": recorded.get("actual_output_tokens"),
            }

    def cancel_operation(self, operation_id, evidence):
        if not isinstance(evidence, str) or not evidence.strip():
            raise IntegrationError("Evidência obrigatória.")

        with self._lock:
            operation = self._operations.get(operation_id)

            if not operation or operation.get("status") != "reserved":
                raise IntegrationError(
                    "Cancelamento permitido somente antes da aprovação."
                )

            try:
                self.budget.release_reservation(operation_id, evidence)
                operation["status"] = "released"
                self._event(
                    operation_id,
                    "reservation_released",
                    {"reason": "local_cancellation"},
                )
            except (BudgetError, AuditError, IntegrationError) as exc:
                operation["status"] = "pending_verification"
                try:
                    self.budget.mark_pending_verification(
                        operation_id,
                        "Falha ao confirmar cancelamento local.",
                    )
                except BudgetError:
                    pass
                raise IntegrationError(
                    "Cancelamento não confirmado; reserva preservada quando possível."
                ) from exc

            return {"operation_id": operation_id, "status": "released"}

    def mark_uncertain(self, operation_id, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise IntegrationError("Motivo da incerteza obrigatório.")

        with self._lock:
            operation = self._operations.get(operation_id)

            if not operation or operation.get("status") not in {
                "approved", "authorization_consumed",
                "transmission_started", "pending_verification"
            }:
                raise IntegrationError("Estado não permite marcar incerteza.")

            self.budget.mark_pending_verification(operation_id, reason)
            operation["status"] = "pending_verification"

            self._event(
                operation_id,
                "result_uncertain",
                {"reason": reason[:500]},
            )

            return {"operation_id": operation_id, "status": "pending_verification"}

    def get_operation_status(self, operation_id):
        with self._lock:
            operation = self._operations.get(operation_id)

            if operation is None:
                raise IntegrationError("Operação não encontrada nesta sessão.")

            return {
                "operation_id": operation_id,
                "status": operation["status"],
                "model": operation.get("model"),
                "payload_sha256": operation.get("payload_sha256"),
            }
