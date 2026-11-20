import json
from pathlib import Path


def carregar_configuracao(caminho):
    arquivo = Path(caminho)

    if not arquivo.exists():
        raise FileNotFoundError("Arquivo de configuracao nao encontrado.")

    with arquivo.open("r", encoding="utf-8") as entrada:
        dados = json.load(entrada)

    if not isinstance(dados, dict):
        raise ValueError("A configuracao deve ser um objeto JSON.")

    return dados


def verificar_campos(dados, campos_obrigatorios):
    ausentes = [
        campo for campo in campos_obrigatorios
        if campo not in dados
    ]

    return {
        "valido": len(ausentes) == 0,
        "campos_ausentes": ausentes
    }
