"""Cliente HTTP mínimo para a OpenAI Responses API.

Não lê arquivos do projeto e não transmite dados sem
uma função de aprovação fornecida pelo coordenador.
"""

import json
import os
import urllib.request


API_URL = "https://api.openai.com/v1/responses"
TIMEOUT_SECONDS = 60
MAX_OUTPUT_TOKENS = 1000
MAX_INPUT_CHARS = 12000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ALLOWED_PAYLOAD_FIELDS = {
    "model", "input", "instructions",
    "max_output_tokens", "store"
}


class ApprovalRequiredError(Exception):
    pass


def create_payload(model, input_text, instructions=None,
                   max_output_tokens=1000):
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Modelo não informado.")

    if not isinstance(input_text, str) or not input_text.strip():
        raise ValueError("Texto de entrada vazio ou inválido.")

    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS
    ):
        raise ValueError("Limite de saída inválido (máximo: 1000).")

    if len(input_text) > MAX_INPUT_CHARS:
        raise ValueError("Entrada excede o limite de caracteres.")

    if instructions is not None and (
        not isinstance(instructions, str)
        or len(instructions) > MAX_INPUT_CHARS
    ):
        raise ValueError("Instruções inválidas ou excessivamente longas.")

    payload = {
        "model": model,
        "input": input_text,
        "max_output_tokens": max_output_tokens,
        "store": False
    }

    if instructions:
        payload["instructions"] = instructions

    return payload


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _mark_uncertain(integration_manager, operation_id, reason):
    try:
        integration_manager.mark_uncertain(operation_id, reason)
    except Exception:
        # Nunca ocultar o erro original nem liberar a reserva.
        pass


def send_approved(integration_manager, operation_id):
    """Transmite somente uma operacao aprovada pelo coordenador."""

    from coordenador.integration_manager import IntegrationManager

    if not isinstance(integration_manager, IntegrationManager):
        raise ApprovalRequiredError(
            "IntegrationManager obrigatorio. Callback arbitrario proibido."
        )

    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ValueError("ID de operacao invalido.")

    # Verificar a credencial antes de consumir a autorizacao.
    api_key = os.environ.get("OPENAI_API_KEY")
    if (
        not isinstance(api_key, str)
        or not api_key.strip()
        or "\r" in api_key
        or "\n" in api_key
    ):
        raise RuntimeError("OPENAI_API_KEY ausente ou invalida.")

    consumed = False

    try:
        # O coordenador fornece exatamente os bytes aprovados.
        serialized = integration_manager.consume_authorization(operation_id)
        consumed = True

        if not isinstance(serialized, bytes):
            raise RuntimeError("Bytes autorizados invalidos.")

        request = urllib.request.Request(
            API_URL,
            data=serialized,
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )

        # Registrar antes de iniciar qualquer conexao.
        integration_manager.mark_transmission_started(operation_id)

        opener = urllib.request.build_opener(_NoRedirectHandler)

        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            content_length = response.headers.get("Content-Length")

            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    raise RuntimeError("Content-Length invalido.") from None

                if declared_size < 0 or declared_size > MAX_RESPONSE_BYTES:
                    raise RuntimeError("Resposta excede o limite de 2 MB.")

            response_data = response.read(MAX_RESPONSE_BYTES + 1)

            if len(response_data) > MAX_RESPONSE_BYTES:
                raise RuntimeError("Resposta excede o limite de 2 MB.")

        try:
            result = json.loads(response_data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError(
                "Resposta da API nao contem JSON valido."
            ) from None

        if not isinstance(result, dict):
            raise RuntimeError("Estrutura de resposta invalida.")

        if result.get("status") != "completed":
            raise RuntimeError("Resposta nao concluiu o processamento.")

        usage = result.get("usage")
        if not isinstance(usage, dict):
            raise RuntimeError("Uso nao informado; verificacao financeira pendente.")

        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")

        if (
            isinstance(input_tokens, bool)
            or not isinstance(input_tokens, int)
            or input_tokens < 0
            or isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or output_tokens < 0
        ):
            raise RuntimeError("Dados de uso invalidos.")

        integration_manager.record_success(
            operation_id,
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )

        return result

    except BaseException as exc:
        if consumed:
            _mark_uncertain(
                integration_manager,
                operation_id,
                "Falha apos consumo da autorizacao: " + type(exc).__name__,
            )
        raise
