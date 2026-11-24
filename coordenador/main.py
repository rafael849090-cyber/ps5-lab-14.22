"""Interface local do laboratorio PS5 firmware 14.22."""

from uuid import uuid4

from coordenador.budget_manager import BudgetManager
from coordenador.audit_logger import AuditLogger, AuditError
from coordenador.approval_manager import ApprovalManager
from coordenador.integration_manager import IntegrationManager
from coordenador.workspace import Workspace, WorkspaceError


class LabConsole:
    def __init__(self):
        self.session_id = "LOCAL-" + uuid4().hex
        self.budget = BudgetManager()
        self.audit = AuditLogger()
        self.approval = ApprovalManager()
        self.integration = IntegrationManager(
            self.budget,
            self.approval,
            self.audit,
        )
        self.workspace = Workspace(self.audit)

        self.audit.record(
            self.session_id,
            "local_session_started",
            {"interface": "terminal", "network_enabled": False},
        )

    def show_status(self):
        status = self.budget.get_budget_status()

        print("\n=== LABORATORIO PS5 FIRMWARE 14.22 ===")
        print("Sessao:", self.session_id)
        print("Mes:", status["month"])
        print("Moeda:", status["currency"])
        print("Limite mensal:", status["limit"])
        print("Gasto registrado:", status["spent"])
        print("Reservado:", status["reserved"])
        print("Disponivel:", status["available"])
        print("API externa: DESATIVADA")
        print("Aprovacao humana: obrigatoria")
        print("======================================\n")

        self.audit.record(
            self.session_id,
            "local_status_viewed",
            {"result": "success"},
        )

    def list_files(self, relative_path="."):
        entries = self.workspace.list_directory(relative_path)

        print("\n=== ARQUIVOS:", relative_path, "===")
        for item in entries:
            prefix = "[DIR]" if item["type"] == "directory" else "[ARQ]"
            print(prefix, item["name"])
        print("Total:", len(entries))

    def read_file(self, relative_path):
        content = self.workspace.read_file(relative_path)

        print("\n=== LEITURA:", relative_path, "===")
        limit = 12000
        print(content[:limit])

        if len(content) > limit:
            print("\n[Exibicao limitada a 12000 caracteres.]")

    def analyze_file(self, relative_path):
        result = self.workspace.analyze_file(relative_path)

        print("\n=== ANALISE:", relative_path, "===")
        for key, value in result.items():
            print(f"{key}: {value}")

    def help(self):
        print("""
COMANDOS DISPONIVEIS

  status                 Consultar estado e orcamento
  arquivos               Listar diretorio principal
  arquivos documentacao  Listar uma pasta
  ler caminho             Ler arquivo autorizado
  analisar caminho        Analisar arquivo
  ajuda                   Mostrar comandos
  sair                    Encerrar interface

Modo atual: operacao local e somente leitura.
Nenhuma transmissao externa e iniciada por esta interface.
""")

    def run(self):
        print("Laboratorio de pesquisa do PS5 firmware 14.22")
        print("Interface local iniciada. Digite ajuda.")

        while True:
            try:
                line = input("PS5-LAB> ").strip()

                if not line:
                    continue

                command, _, argument = line.partition(" ")
                command = command.lower()
                argument = argument.strip()

                if command == "status":
                    self.show_status()

                elif command in ("ajuda", "help"):
                    self.help()

                elif command == "arquivos":
                    self.list_files(argument or ".")

                elif command == "ler":
                    if not argument:
                        print("Uso: ler caminho")
                    else:
                        self.read_file(argument)

                elif command == "analisar":
                    if not argument:
                        print("Uso: analisar caminho")
                    else:
                        self.analyze_file(argument)

                elif command in ("sair", "exit", "quit"):
                    self.audit.record(
                        self.session_id,
                        "local_session_ended",
                        {"result": "normal"},
                    )
                    print("Sessao encerrada.")
                    break

                else:
                    print("Comando desconhecido. Digite ajuda.")

            except (WorkspaceError, AuditError) as exc:
                print("OPERACAO BLOQUEADA:", str(exc))

            except EOFError:
                print("\nEntrada encerrada.")
                break

            except KeyboardInterrupt:
                print("\nInterrupcao recebida. Encerrando.")
                break


def main():
    console = LabConsole()
    console.run()


if __name__ == "__main__":
    main()
