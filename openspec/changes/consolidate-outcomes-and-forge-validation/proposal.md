# Proposal

## Why

Três verificações offline reproduziram divergências entre a validação e a execução do Forge, a classificação de um candle sem movimento e a contagem de resultados no relatório diário. Uma regra compartilhada em cada fronteira evita que a mesma entrada receba respostas contraditórias.

## What Changes

- Distinguir validação estrutural de elegibilidade de execução do Forge. Rejeitar propostas que não tenham rota suportada para um experimento, ainda sem abrir dados ou treinar; preservar as restrições atuais das rotas.
- Unificar a leitura do movimento da vela M1 sem inferir a liquidação: abertura igual ao fechamento descreve uma vela sem movimento. A ordem executada segue o resultado confirmado pela Ebinex; `REFUNDED`, `TIE` e `LOSS` são estados distintos. Preservar os resultados simulados, os rótulos binários e o replay legado até haver evidência para revisar a política de simulação.
- Normalizar `LOSE` para `LOSS` na leitura do relatório diário e incluir `TIE` na visão por contexto. Manter `REFUNDED` como categoria separada no total, sem contá-la como vitória, perda ou empate. Não reescrever nem reclassificar registros históricos.
- Não alterar o worker, a política financeira, a configuração, os formatos persistidos ou a autorização de ordens. A análise do histórico TEST sem filtro de status é um passo separado; a implementação desta refatoração não consulta a corretora.

## Capabilities

### New Capabilities

- `forge-experiment-validation`: validação estrutural e de elegibilidade de rotas de experimentos.
- `m1-directional-outcomes`: leitura compartilhada de preços M1, distinta da liquidação confirmada e das políticas de simulação existentes.
- `daily-outcome-reporting`: agregação diária consistente de resultados por total e contexto.

### Modified Capabilities

Nenhuma spec principal existe para esses comportamentos.

## Impact

Três frentes com arquivos de escrita explícitos no design. Propostas com experimentos estruturalmente válidos, mas sem nenhuma rota suportada, passam a falhar na validação. O relatório diário preserva o formato, agrupa `LOSE` em `LOSS`, inclui empates nos contextos e mantém devoluções separadas no total. Sem dependências de produção novas ou migração de estado.
