# Resultados no relatório diário

## Purpose

Reconciliar os resultados do total diário com os respectivos contextos, manter devoluções separadas e preservar os registros originais do SQLite.

## ADDED Requirements

### Requirement: Agregação canônica de resultados financeiros
O relatório diário SHALL contar `LOSS` e `LOSE` sob `LOSS`, tanto no total quanto no contexto existente de ativo, direção e faixa de ADX. Resultados `TIE` SHALL aparecer nas duas visões. A normalização MUST NOT alterar os registros no SQLite.

#### Scenario: Grafias mistas e empate no mesmo contexto
- **WHEN** o mesmo dia e contexto contêm `LOSS`, `LOSE` e `TIE`
- **THEN** tanto o total quanto o contexto exibem `LOSS=2` e `TIE=1`, sem chave `LOSE`
- **AND** o valor original `LOSE` permanece no SQLite

### Requirement: Preservação do escopo diário
O relatório SHALL aplicar o mesmo recorte diário e as mesmas chaves de saída e de contexto. Estados técnicos SHALL continuar no total, sem entrar em um agrupamento de resultados financeiros.

#### Scenario: Estado técnico e limites do recorte
- **WHEN** o dia solicitado contém um estado técnico e resultados de diferentes contextos, e outro dia tem registros
- **THEN** o estado técnico aparece apenas no total, os contextos não são mesclados e as linhas do outro dia não são contadas

### Requirement: Devolução é categoria separada
O relatório SHALL manter `REFUNDED` sob sua própria chave no total, fora do agrupamento por contexto de `WIN`, `LOSS` e `TIE`. O relatório MUST NOT inferir status a partir de preços ou contar devolução como vitória, perda ou empate.

#### Scenario: Devolução ao lado de perdas e empate
- **WHEN** os registros do mesmo dia e contexto são `LOSS`, `LOSE`, `TIE` e `REFUNDED`
- **THEN** o total exibe `LOSS=2`, `TIE=1` e `REFUNDED=1`; o contexto exibe `LOSS=2` e `TIE=1`
- **AND** os quatro estados originais permanecem no SQLite
