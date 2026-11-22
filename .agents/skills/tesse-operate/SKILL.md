---
name: tesse-operate
description: "Use when operating the Tesse Ebinex TEST worker: configuration, monitoring, orders, accounting or rollback. Also for explicit Binance history sync or native Ebinex history probe, sync, or job status. Not for starting Forge research or filling a missing snapshot pin by default."
---

# Operar o robô Tesse em TEST

Opere o fluxo baseado em `bot.yml`, SQLite e SDK Ebinex, preservando a identidade da conta, das ordens e das sessões. O executor suporta apenas TEST/OPTION/M1; não existe modo REAL para habilitar. Pesquisa e confirmação offline do Forge não são operação TEST; para conduzir uma iteração, use `tesse-iterate`.

## Localizar o estado e escolher a ação

Use o entry point `tesse` do ambiente do projeto (Python 3.14+ e SDK instalado), ou `python -m tesse` com o mesmo interpretador. Não dependa dos wrappers PowerShell. Prefira `tesse --config "caminho/bot.yml" <comando>`; `runtime.data_directory` relativo é resolvido a partir do diretório de `bot.yml`.

Leia [recuperação e estado](references/runtime-and-recovery.md) antes de tratar pendências, autenticação, cofre ou troca de versão. Para [dados históricos](references/historical-data.md), diferencie cache público Binance de fila nativa Ebinex. Enfileire somente por pedido explícito com escopo definido. Consultar um job não autoriza outro job, login, restart ou ordem. A falta de pin experimental não autoriza baixar dados nem alterar o plano do Forge.

| Pedido | Ação operacional |
| --- | --- |
| Consultar estado do worker | `status`, `versions`; inspecionar logs pertinentes sem segredos |
| Sondar, coletar ou consultar histórico nativo | Fila `data ebinex`; acompanhe pelo ID, não pelo `status` do worker |
| Acompanhar mercado sem ordens | `start --observe-only`; encerrar com `stop` |
| Iniciar execução TEST solicitada | `start` somente quando o gate TEST permitir; o worker não agenda revisão |
| Parar novas entradas e acompanhar a pendente | `stop --wait 180`, depois conferir estado e sessão |
| Gerar contabilidade diária | `report` grava relatório; não é consulta sem efeitos |
| Restaurar perfil | `rollback` com worker parado e sem sessão aberta ou ordem pendente |

Mesmo `status` e `versions` passam pelo Store e podem criar SQLite/baseline. Se só pediram consulta e não há `state.sqlite` no diretório efetivo, não execute esses comandos, `init` ou `start`. Informe que não há estado nessa instalação; isso não demonstra que outro worker está parado.

## Preservar os limites do pedido

`init` é preparação explícita, não reparo universal. `login` usa entrada oculta no terminal e exige worker parado. Nunca solicite senha ou chave pelo chat, grave-as no YAML ou mostre arquivos privados; se a entrada interativa não estiver disponível, oriente o login no terminal.

Não reinicie para apagar incerteza, recrie chave perdida ou remova journals. Reconcilie ordens pela identidade original e preserve o bloqueio quando o resultado continuar desconhecido.

Relate worker, conta TEST identificada sem segredos, versão/piloto, sessão ou ordem pendente, ação executada e bloqueios restantes. Uma sessão Soros aberta pode sobreviver à parada do processo; informe o estado real em vez de declarar encerramento apenas porque o worker parou.
