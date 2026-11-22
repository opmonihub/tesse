# Design

## Context

`validate_documents` compila o grafo e verifica o contrato gerenciado, mas a restrição de componentes por rota vive em `forge/graph.py` e é aplicada mais tarde. A comparação de preços M1 também tem implementações locais; `normalize_outcome` já oferece o vocabulário canônico para a contabilidade. No recorte de devoluções fornecido pelo usuário, três linhas M1 exibem preços iguais e estorno integral. É evidência de que ambos podem coexistir, não de frequência ou de uma regra geral: o recorte só contém devoluções e mostra preços formatados. Ver `proposal.md` e as specs para os resultados esperados.

## Goals / Non-Goals

**Goals:** usar uma regra de elegibilidade para a consulta documental e para uma execução concreta; compartilhar a comparação de preços M1 sem inferir liquidação; reconciliar contagens na leitura sem confundir devolução com empate ou perda. Dar a cada agente paths de escrita exclusivos.

**Non-Goals:** reformular a CLI ou as fases do Forge, alterar o alvo binário, mudar a política de liquidação, editar `worker.py` ou Store, migrar artefatos, alterar `bot.yml` ou acessar credenciais, dados reais e corretora.

## Decisions

1. **Forge: uma política, duas consultas.** Manter a política de restrição de componentes em `tesse/forge/graph.py`. Uma consulta pura deriva da presença de linhagem gerenciada e dos componentes quais rotas não são barradas por essa política; `require_supported_route` usa a mesma regra para a rota efetivamente solicitada. Em `validation.py`, consultar a viabilidade após compilar e validar o contrato gerenciado. Sem rota, apontar o experimento e a restrição; com rota condicional, não exigir um estudo só para validar o documento. As demais pré-condições existentes continuam onde estão. Manter a interface de `require_supported_route` se possível para não editar runner, iteração ou dispatch; `forge/cli.py` só é elegível para mudança se necessária para transmitir o contexto já conhecido. Alternativa rejeitada: copiar as condicionais de XGBoost para a validação ou empurrar todo diagnóstico para `run`.
2. **M1: separar movimento, projeção e liquidação.** Definir em `tesse/core.py` uma comparação pura de abertura e fechamento (alta, baixa ou igualdade), sem emitir `WIN`, `LOSS`, `TIE` ou `REFUNDED`. Reutilizar essa comparação onde houver classificação escalar em core, Forge, shadow e verificação demo offline. Cada consumidor mantém sua projeção atual, incluindo a assinatura e o retorno existentes dos helpers e o `LOSE` legado; `classify_canonical` baseado em preço não é autoridade de liquidação. O rótulo vetorizado de treino continua inalterado. Uma ordem executada usa o status confirmado pela Ebinex, sem consultar candles para substituí-lo. A fronteira operacional já existente não precisa mudar. Alternativa rejeitada: mapear toda igualdade para `TIE`, `LOSS` ou `REFUNDED` com base no recorte.
3. **Relatório: normalização apenas de leitura.** Em `tesse/cli.py`, usar a normalização existente em `tesse/outcomes.py` uma vez por linha e usar o resultado tanto no total quanto no agrupamento. Preservar `REFUNDED` sob sua própria chave no total, fora do agrupamento de `WIN`, `LOSS` e `TIE`. Preservar no total também os demais estados técnicos e eventuais valores históricos desconhecidos. Não reconstruir status ou saldo a partir do preço ou do valor estornado. Não adicionar um segundo mapa de aliases nem mudar o relatório geral do Store. Alternativa rejeitada: reescrever o banco ou corrigir apenas o agrupamento.

### Escritura por agente

| Frente | Arquivos permitidos | Interface com outras frentes |
| --- | --- | --- |
| Forge | `tesse/forge/graph.py`, `tesse/forge/validation.py`, `tesse/forge/cli.py` se necessário, `tests/test_forge_route_validation.py` | Não editar `tesse/forge/components.py`; o classificador não altera os contratos de grafo. |
| Movimento M1 | `tesse/core.py`, `tesse/forge/components.py`, `tesse/execution/shadow.py`, `tesse/demo_dmi.py`, `tests/test_m1_outcomes.py` | `tesse/outcomes.py` é só leitura; não alterar o vocabulário persistido nem o alias existente. |
| Relatório | `tesse/cli.py`, `tests/test_daily_report.py` | Reutilizar o normalizador público atual, sem depender da nova classificação M1. |

O coordenador integra e atualiza `tasks.md`. Nenhum agente toca nos arquivos removidos anteriormente no checkout nem em alterações de outra frente. Se for preciso sair da lista de arquivos, pausar e revisar o escopo antes de editar.

## Risks / Trade-offs

- [Quebrar propostas que antes validavam] → restringir o novo erro a experimentos sem rota possível; cobrir documentos que têm somente rota condicional e as rotas HGB/DMI já elegíveis.
- [Confundir simulação com liquidação] → testar alta/baixa/igualdade no comparador de preços e preservar as saídas atuais do replay, shadow, demo, helpers e alvo binário. Não usar preço para substituir status confirmado nem preencher status ausente.
- [Mudar contabilidade persistida ou alarmes técnicos] → testar com SQLite em memória, incluindo `REFUNDED`, um estado técnico e a exclusão de outro dia; verificar a preservação dos valores originais na tabela.

## Migration Plan

Não há migração de dados. Notificar a mudança nas chaves e contagens diárias e na validação de propostas sem rota. Reverter apenas o patch de código restaura o comportamento anterior sem alterar registros existentes.

## Open Questions

A política de liquidação simulada para preços iguais fica fora desta implementação. Em uma análise TEST autorizada separadamente, comparar histórico M1 sem filtro de status com os preços nativos das ordens correspondentes. Parear pela identidade da ordem e conferir ativo, direção e horário de entrada aceito; registrar cobertura, precisão dos preços e casos inconclusivos. Só propor mudança de política após evidência suficiente; o recorte atual não resolve a questão.
