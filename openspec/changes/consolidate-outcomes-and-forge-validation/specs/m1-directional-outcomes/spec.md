# Movimento da vela e resultados M1

## Purpose

Distinguir o movimento observado da vela M1, o resultado projetado em simulação e a liquidação efetivamente confirmada pela Ebinex.

## ADDED Requirements

### Requirement: Observação de preços M1
Dadas a abertura e o fechamento, a observação SHALL distinguir alta, baixa e ausência de movimento. A igualdade dos preços MUST NOT determinar o status financeiro de uma ordem.

#### Scenario: Candle sem movimento
- **WHEN** o preço de fechamento é igual ao de abertura
- **THEN** a observação indica ausência de movimento tanto para CALL quanto para PUT, sem inferir `TIE`, `LOSS` ou `REFUNDED`

#### Scenario: Candle com movimento
- **WHEN** o fechamento excede ou fica abaixo da abertura
- **THEN** a observação indica, respectivamente, alta ou baixa, independentemente da direção da ordem

### Requirement: Autoridade do resultado executado
Para uma ordem executada, o resultado financeiro SHALL seguir o status confirmado para a ordem pela Ebinex. Preços da vela MUST NOT substituir o status da ordem nem preencher uma liquidação não confirmada. `REFUNDED` SHALL permanecer distinto de `TIE` e `LOSS`.

#### Scenario: Devolução integral confirmada
- **WHEN** o histórico confirma `REFUNDED` e a devolução integral da entrada
- **THEN** o registro é de devolução, não de vitória, perda ou empate, e o valor devolvido não é lucro

#### Scenario: Liquidação não comprovada pelos preços
- **WHEN** a vela não se moveu, mas não existe confirmação de liquidação para a ordem
- **THEN** os preços sozinhos não permitem classificar financeiramente a ordem

### Requirement: Compatibilidade das simulações M1
A refatoração MUST preservar as projeções existentes de Forge, shadow, verificação demo offline e replay legado, os formatos de artefatos e os rótulos de treino. Uma nova política para liquidação simulada em casos de preços iguais requer evidência operacional e aprovação separadas.

#### Scenario: Projeções atuais quando o preço não se move
- **WHEN** um consumidor de simulação processa uma vela com preços iguais, sem confirmação de uma ordem da Ebinex
- **THEN** a saída simulada segue sua política existente, incluindo `LOSE` no replay legado, sem ser apresentada como liquidação operacional comprovada

#### Scenario: Forge gera rótulos de treino
- **WHEN** um exemplo com resultado disponível é projetado no alvo binário do Forge
- **THEN** apenas uma vitória pela política de simulação vigente gera `1`; as demais observações elegíveis continuam gerando `0`
