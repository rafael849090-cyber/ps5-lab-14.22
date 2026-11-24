"""Autorização humana explícita, vinculada e de uso único."""

import hashlib
import json
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from uuid import uuid4


EXPECTED_ENDPOINT = "https://api.openai.com/v1/responses"


class ApprovalError(Exception):
    """Erro de autorização."""


@dataclass(frozen=True)
class Authorization:
    authorization_id: str
    operation_id: str
    payload_sha256: str
    model: str
    endpoint: str
    purpose: str
    serialized_payload: bytes


class ApprovalManager:
    def __init__(self):
        self._issued = {}
        self._operations = {}
        self._lock = threading.Lock()

    @staticmethod
    def _serialize(payload):
        if not isinstance(payload, dict):
            raise ApprovalError("Payload inválido.")

        try:
            return json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ApprovalError("Payload não serializável.") from exc

    @staticmethod
    def _validate_amount(value, label):
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise ApprovalError(label + " inválido.")

        if not amount.is_finite() or amount < 0:
            raise ApprovalError(label + " inválido.")

        return str(amount)

    def request_approval(
        self,
        operation_id,
        payload,
        endpoint,
        purpose,
        estimated_input_tokens,
        estimated_output_tokens,
        estimated_cost,
        reserve_amount,
    ):
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ApprovalError("Identificador da operação obrigatório.")

        if endpoint != EXPECTED_ENDPOINT:
            raise ApprovalError("Destino não autorizado.")

        if not isinstance(purpose, str) or not purpose.strip():
            raise ApprovalError("Finalidade obrigatória.")

        if not isinstance(payload, dict):
            raise ApprovalError("Payload inválido.")

        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ApprovalError("Modelo inválido.")

        for name, value in (
            ("tokens de entrada", estimated_input_tokens),
            ("tokens de saída", estimated_output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ApprovalError("Estimativa de " + name + " inválida.")

        estimated_cost = self._validate_amount(
            estimated_cost, "Custo estimado"
        )
        reserve_amount = self._validate_amount(
            reserve_amount, "Reserva financeira"
        )

        serialized = self._serialize(payload)
        display_payload = json.loads(serialized.decode("utf-8"))
        digest = hashlib.sha256(serialized).hexdigest()
        authorization_id = str(uuid4())

        # Reserva o ID antes de solicitar a decisão humana.
        with self._lock:
            if operation_id in self._operations:
                raise ApprovalError(
                    "Operação duplicada ou já encerrada neste gerenciador."
                )

            self._operations[operation_id] = {
                "status": "pending",
                "authorization_id": authorization_id,
            }

        try:
            print("\n=== APROVAÇÃO HUMANA OBRIGATÓRIA ===")
            print("OPERAÇÃO:", operation_id)
            print("AUTORIZAÇÃO:", authorization_id)
            print("MODELO:", model)
            print("DESTINO:", endpoint)
            print("FINALIDADE:", purpose)
            print("TOKENS DE ENTRADA ESTIMADOS:", estimated_input_tokens)
            print("TOKENS DE SAÍDA ESTIMADOS:", estimated_output_tokens)
            print("CUSTO ESTIMADO (USD):", estimated_cost)
            print("RESERVA ORÇAMENTÁRIA (USD):", reserve_amount)
            print("SHA-256:", digest)
            print("\nPAYLOAD INTEGRAL:")
            print(json.dumps(display_payload, ensure_ascii=False, indent=2))
            print("\nA autorização vale somente para esta operação e este conteúdo.")
            print("Digite APROVAR para autorizar.")

            decision = input("Decisão: ").strip()

            if decision != "APROVAR":
                with self._lock:
                    self._operations[operation_id]["status"] = "denied"
                raise ApprovalError("Transmissão não autorizada.")

            authorization = Authorization(
                authorization_id=authorization_id,
                operation_id=operation_id,
                payload_sha256=digest,
                model=model,
                endpoint=endpoint,
                purpose=purpose,
                serialized_payload=serialized,
            )

            with self._lock:
                state = self._operations.get(operation_id)

                if (
                    state is None
                    or state["authorization_id"] != authorization_id
                    or state["status"] != "pending"
                ):
                    raise ApprovalError("Estado da operação foi alterado.")

                self._issued[authorization_id] = {
                    "authorization": authorization,
                    "status": "approved",
                }
                state["status"] = "approved"

            return authorization

        except BaseException:
            with self._lock:
                state = self._operations.get(operation_id)
                if (
                    state is not None
                    and state["authorization_id"] == authorization_id
                    and state["status"] == "pending"
                ):
                    state["status"] = "cancelled"
            raise

    def consume(
        self,
        authorization,
        operation_id,
        payload,
        endpoint,
        purpose,
    ):
        if not isinstance(authorization, Authorization):
            raise ApprovalError("Objeto de autorização inválido.")

        with self._lock:
            record = self._issued.get(authorization.authorization_id)

            if record is None or record["status"] != "approved":
                raise ApprovalError(
                    "Autorização desconhecida, consumida ou encerrada."
                )

            expected = record["authorization"]
            serialized = self._serialize(payload)
            digest = hashlib.sha256(serialized).hexdigest()

            checks = (
                authorization == expected,
                operation_id == expected.operation_id,
                endpoint == expected.endpoint,
                purpose == expected.purpose,
                payload.get("model") == expected.model,
                digest == expected.payload_sha256,
                serialized == expected.serialized_payload,
            )

            if not all(checks):
                raise ApprovalError("Dados não correspondem à autorização.")

            # Consumo e alteração do estado ocorrem sob a mesma trava.
            record["status"] = "consumed"
            self._operations[operation_id]["status"] = "consumed"

            return expected.serialized_payload
