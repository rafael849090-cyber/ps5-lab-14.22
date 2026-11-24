# Laboratório de Pesquisa — PS5 Firmware 14.22

Pesquisa técnica organizada sobre o firmware 26.07-14.22.00 da PlayStation 5 (Android + Termux).

## Objetivo
- Documentar informações oficiais e públicas sobre o firmware 14.22
- Organizar evidências de forma rastreável
- Separar fatos de hipóteses
- Manter controle humano obrigatório

## Estrutura
- codigo/          → scripts
- coordenador/     → sistema de coordenação
- documentacao/    → agentes e políticas
- evidencias/      → capturas e fontes
- pesquisas/       → notas de pesquisa
- relatorios/      → T01, T02...

## Status (24/11/2026)
- T01 concluído
- Arquitetura aprovada
- Coordenador em modo local (somente leitura)
- Agentes definidos, mas ainda não integrados
- Pastas de trabalho ainda vazias

## Políticas
- Preservar originais
- Registrar fontes
- Separar fatos de hipóteses
- Revisão humana obrigatória
- Sem execução automática de código não confiável
- Orçamento limitado a US$ 10/mês

## Iniciar interface local
source .venv/bin/activate
python -m coordenador.main
