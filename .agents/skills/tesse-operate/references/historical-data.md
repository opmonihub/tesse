# Dados históricos

## Cache público da Binance

`tesse data sync` usa `runtime.data_directory` do `bot.yml`. Para sincronizar um
ativo em um intervalo explicitamente solicitado:

```text
tesse --config "bot.yml" data sync --symbol BTCUSDT --start 2026-10-01 --end 2026-10-02
```

Use dias UTC completos e encerrados, com início inclusivo e fim exclusivo.
Sem ativo, confira o alcance antes de sincronizar os ativos configurados.
O cache usa ZIPs públicos da Binance, checksums, arrays NPY e manifests JSON com
identidade por snapshot. Não preencha lacunas nem invente candles quando o dia
anterior não tiver sido publicado. Preserve os arquivos originais se forem
necessários para reproduzir uma avaliação. A sincronização não autoriza iniciar
o worker.

## Fila de histórico nativo Ebinex (TEST)

Use a mesma configuração e `runtime.data_directory` do worker. `probe` e
`sync` enfileiram e retornam um ID; não conectam ao SDK nem consultam o Store
operacional no CLI. Enfileire somente com pedido explícito, símbolo nativo (por
exemplo, `BTCUSDX`, não `BTCUSDT`) e fronteira ou intervalo UTC. `--symbol` é
repetível.

```sh
tesse --config "bot.yml" data ebinex probe --symbol BTCUSDX --as-of 2026-11-21
tesse --config "bot.yml" data ebinex sync --symbol BTCUSDX --start 2026-11-01 --end 2026-11-21
tesse --config "bot.yml" data ebinex status "$JOB_ID"
```

`--end` é exclusivo; use apenas dias UTC encerrados. Consulte `status` do job na
mesma raiz, sem criar a fila se ela não existir. Informe ID, `state`, `reason`,
`confirmed_pages` e os resultados de `report` por símbolo. Uma solicitação
equivalente reutiliza o job ativo; após estado terminal, pode criar outro. O
worker TEST já autenticado atende a fila em baixa prioridade e pode retomar o
checkpoint na mesma conta. Se aguarda worker ou sessão, relate o bloqueio; não reenvie por conta própria, não faça login, não
reinicie, não limpe estado e não dispare ordens ou drill para liberar o job. Em
`account_changed`, valide a instalação e a identidade antes de outro pedido.

Uma sondagem é amostral. Só uma série `complete` pode publicar snapshot;
`partial`, `no_data` e `inconclusive` não provam cobertura contínua nem autorizam
preencher lacunas com proxy. Para a autoria, entregue qualidade, intervalo UTC,
lacunas e, se publicado, `report.<símbolo>.snapshot` (`manifest`,
`manifest_sha256`, `sha256`). Para interpretação e recuperação, leia o
[runbook de histórico nativo](../../../../docs/ebinex-history.md). Uma saída
íntegra não comprova 90 dias de prontidão nem paridade de liquidação.
