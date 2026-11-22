# Validação de experimentos Forge

## Purpose

Identificar, sem acessar os candles, quando a declaração de um experimento não permite nenhuma rota de execução suportada pelo Forge.

## ADDED Requirements

### Requirement: Viabilidade documental do experimento
A validação documental SHALL verificar a estrutura e rejeitar experimentos para os quais não exista rota suportada. Ela SHALL poder validar um experimento com rota condicional sem uma declaração `--study`. Validar um documento não autoriza uma execução.

#### Scenario: XGBoost declarado sem modo gerenciado
- **WHEN** uma proposta estruturalmente válida declara XGBoost sem modo gerenciado
- **THEN** a validação falha indicando que o experimento não tem rota suportada, sem abrir snapshots nem treinar

#### Scenario: Declaração elegível para um estudo específico
- **WHEN** o documento é válido e só pode ser executado como estudo avulso de desenvolvimento gerenciado
- **THEN** a validação documental pode ser concluída sem a declaração do estudo e sem iniciar uma execução

### Requirement: Elegibilidade da rota de execução
O Forge MUST aplicar a mesma política de elegibilidade na validação documental e ao avaliar uma rota concreta. XGBoost só é elegível em estudo avulso de desenvolvimento gerenciado; o score de volatilidade também exige XGBoost.

#### Scenario: Estudo avulso com XGBoost
- **WHEN** a rota solicitada é um estudo avulso de desenvolvimento gerenciado com XGBoost e as demais pré-condições são atendidas
- **THEN** a política permite essa rota com ou sem o score de volatilidade

#### Scenario: XGBoost fora da rota suportada
- **WHEN** o usuário solicita execução de XGBoost sem estudo avulso de desenvolvimento gerenciado, inclusive iteração gerenciada ou confirmação
- **THEN** a rota é rejeitada antes de executar o grafo

#### Scenario: Volatilidade sem XGBoost
- **WHEN** um experimento declara o score de volatilidade sem XGBoost
- **THEN** a política não considera o experimento elegível

#### Scenario: Rotas existentes sem componentes restritos
- **WHEN** um experimento HGB ou DMI era elegível para sua rota e não usa componentes restritos
- **THEN** a consolidação mantém essa elegibilidade e as demais pré-condições da rota
