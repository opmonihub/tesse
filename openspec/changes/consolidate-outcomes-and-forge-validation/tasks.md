# Tasks

## 1. Elegibilidade Forge — agente Forge

- [x] 1.1 Criar regressões offline em `tests/test_forge_route_validation.py` para o grafo XGBoost sem `managed` estruturalmente válido, XGBoost com e sem volatilidade na rota avulsa gerenciada, rejeições fora dela, volatilidade sem XGBoost e preservação de HGB/DMI. Consolidar a regra em `tesse/forge/graph.py` e inserir a checagem documental em `tesse/forge/validation.py`; mexer em `tesse/forge/cli.py` só se necessário. Verificar com `python -m pytest tests/test_forge_route_validation.py`, sem abrir candles ou treinar.

## 2. Observação de preços — agente M1

- [x] 2.1 Criar `tests/test_m1_outcomes.py` para alta, baixa e igualdade na comparação pura de preços, preservação das projeções CALL/PUT dos consumidores existentes e do rótulo binário. Consolidar a comparação em `tesse/core.py` e reutilizá-la nos comparadores escalares delimitados pelo design, sem promover preço a status de ordem nem alterar o worker. Verificar com `python -m pytest tests/test_m1_outcomes.py` e fixtures offline, sem consultar a corretora.

## 3. Agregação — agente relatório

- [x] 3.1 Criar `tests/test_daily_report.py` com SQLite em memória para grafias mistas, empate, devolução, estados técnicos, contextos e recorte diário. Normalizar apenas a leitura de `daily_report` em `tesse/cli.py`. Verificar com `python -m pytest tests/test_daily_report.py`: `LOSS=2` e `TIE=1` no total e no grupo, `REFUNDED=1` somente no total, valores de origem intactos.

## 4. Integração — coordenador

- [x] 4.1 Conferir o diff contra o escopo de escrita de cada frente, executar `python -m pytest` no ambiente Python 3.14 e `ruff check` nos arquivos de código/teste editados, e validar com `openspec validate consolidate-outcomes-and-forge-validation --strict`. Não iniciar worker, sessão ou ordem TEST; separar quaisquer falhas de baseline dos resultados das novas regressões.

## Workflow follow-up

- Antes de propor uma política nova de liquidação simulada para preços iguais, solicitar separadamente a comparação do histórico de ordens M1 TEST sem filtro de status com os preços nativos correspondentes. Não executar essa consulta como parte do apply desta refatoração.
