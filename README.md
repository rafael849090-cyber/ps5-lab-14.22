# Laboratório de Pesquisa — PS5 Firmware 14.22

[![Status](https://img.shields.io/badge/status-em%20andamento-yellow)](https://github.com/rafael849090-cyber/ps5-lab-14.22)
[![Firmware](https://img.shields.io/badge/firmware-14.22-blue)]()
[![Platform](https://img.shields.io/badge/platform-Termux%20%7C%20Android-green)]()

Pesquisa técnica organizada sobre o firmware **26.07-14.22.00** da PlayStation 5.

## Objetivo

- Documentar informações oficiais e públicas
- Separar rigorosamente **fatos** de **hipóteses**
- Manter rastreabilidade e revisão humana obrigatória
- Preparar infraestrutura para coordenação de agentes de IA

## Estrutura

| Pasta | Descrição |
|-------|---------|
| `coordenador/` | Sistema de coordenação local (Python) |
| `documentacao/` | Agentes, arquitetura e políticas |
| `relatorios/` | Relatórios técnicos (T01, T02…) |
| `pesquisas/` | Notas de pesquisa |
| `evidencias/` | Capturas e fontes primárias |
| `codigo/` | Scripts e ferramentas |

## Status atual

- T01 e T02 concluídos
- Arquitetura e políticas definidas
- Coordenador em modo local (somente leitura)
- Nenhum firmware baixado ou analisado

## Políticas

- Preservar arquivos originais
- Registrar todas as fontes
- Revisão humana obrigatória
- Sem execução automática de código não confiável
- Orçamento limitado

## Como usar localmente

```bash
cd \~/ps5-lab-14.22
source .venv/bin/activate
python -m coordenador.main
