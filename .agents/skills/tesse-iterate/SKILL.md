---
name: tesse-iterate
description: Use when asked to conduct Tesse Forge offline research end to end, start or resume an iteration, execute a standalone study, record a round review, approve an adjustment, or freeze or confirm a candidate. Not for evidence-only requests, acquiring data, archiving, promotion, or worker operation.
---

# Conduzir uma iteração no Tesse Forge

**Pesquisa offline.** Use a CLI do ambiente Tesse e o contrato em [docs/forge.md](../../../docs/forge.md#conduzir-o-piloto-sem-scripts-por-rodada). Identifique a raiz (`forge/` por padrão), proposta, experimento e iteração ou estudo exatos. Se o pedido é só de diagnóstico, parecer ou minuta, use `tesse-evidence` e não altere o estado.

## Condução delegada, sem pausas de rotina

- Um pedido explícito para conduzir uma iteração de desenvolvimento de ponta a ponta autoriza o agente a diagnosticar, redigir e registrar as revisões daquela iteração e retomar os passos liberados pelo plano, sem perguntar novamente por cada YAML ou retomada. Use `tesse-evidence` para produzir a revisão e registre-a pelo comando desta skill. Siga `next_action` com a revisão de estado corrente até a meta, parada pelo plano/revisão ou um bloqueio real. Reporte o resultado e as decisões tomadas. Não estenda a autorização a outro alvo.
- Ao preparar um **novo** plano para condução delegada de ponta a ponta, declare `budget.automatic: true` se o usuário não exigir aprovação manual. Num plano congelado com essa opção, deixe o Forge aprovar os ajustes elegíveis via `--resume`; não peça aprovação por ajuste nem invoque `--approve-adjustment`. Preserve um plano fornecido ou congelado; se ele exigir aprovação manual, pare diante do ajuste exato em `approval_required` em vez de reescrever o plano ou aprovar por conta própria.
- Para um **estudo avulso**, um pedido de ponta a ponta que inclua registrar a revisão autoriza execução, diagnóstico e registro para o estudo identificado, sem confirmação intermediária. “Rode e me diga o resultado” permite executar e reportar, não registrar. Não inicie outro estudo só porque a revisão sugeriu uma investigação.
- Revisão válida continua obrigatória: nunca pule o diagnóstico, invente uma conclusão ou ignore orçamento, bloqueios, revisão de estado ou seleção explícita de dados. A delegação termina no desenvolvimento offline. Ao encerrar, informe a elegibilidade, mas não peça aprovação para iniciar a confirmação. Congelamento e confirmação final só entram no fluxo se solicitados separadamente. Publicação de conhecimento, arquivamento, promoção e operação TEST também estão fora do escopo.

## Começar ou retomar

- Crie só quando solicitado, com `tesse forge iterate "$PROPOSAL" --root "$WORKSPACE" --experiment "$EXPERIMENT" --plan "$PLAN" --json`. O plano v2 inclui o estudo inicial, orçamento, blocos congelados, allowlist e pin confirmatório separado e posterior. Se faltar o plano ou um pin obrigatório, informe qual falta. Não troque o pin pelo cache mais recente, não fabrique hashes e não sincronize dados para desbloquear a rodada. A sincronização é um pedido separado, com ativo e intervalo definidos. Use `tesse-propose` para formular a declaração, não para iniciar a rodada.
- Na execução, use o `data_root` do workspace ou `--data-root` explícito onde os caminhos dos manifestos já fixados resolvem. Para snapshot nativo, é a raiz que contém `market-data/ebinex/`, não a pasta do snapshot. Preserve a raiz efetiva da iteração na retomada.
- Para o ciclo gerenciado, comece sempre por inspecionar sem ação:

  ```sh
  tesse forge iterate "$PROPOSAL" --root "$WORKSPACE" --iteration "$ITERATION" --json
  ```

- Confirme `schema_version: 1`, as identidades e `state_revision`. Use **uma** ação por chamada; para alterar uma iteração existente, acrescente `--state-revision "$REV"` da inspeção corrente. Não passe essa opção na criação. Em erro, leia também o envelope JSON de saída: pode vir com código 1. Se a resposta se perdeu ou a revisão ficou obsoleta, reinspecione a mesma iteração sem ação, confira identidade/evidência e siga `next_action` somente sob a autorização vigente. Não reaplique uma aprovação pontual a outra rodada; pare se a publicação ou o crítico continuar não verificável. Para experimentos legados, consulte o fluxo legado em `docs/forge.md`; não trate ausência do envelope v1 como permissão para avançar.

| `next_action.id` / autorização | O que fazer |
| --- | --- |
| `resume` / `ready` | Use `--resume` se o pedido autoriza avançar. Pode reconciliar ou despachar; não é inspeção. |
| `review_round` / `review_required` | Verifique o diagnóstico e redija a revisão com `tesse-evidence`. Com condução delegada, registre a revisão válida sem nova autorização, reinspecione e siga a ação liberada; sem essa delegação, registre apenas o YAML exato aprovado. |
| `approve_adjustment` / `approval_required` | Se o plano exige aprovação manual, só use `--approve-adjustment "$ADJUSTMENT_JSON"` após autorização do ajuste exato e do despacho que ele pode causar. Use `arguments.adjustment` e a revisão de estado corrente. Num plano automático, um pedido inesperado de aprovação é motivo para verificar o plano/estado, não para forçar o comando. |
| `retry_round` / `ready` | Use `--retry-study "$STUDY"` só com pedido explícito, falha comprovada antes da publicação, orçamento e declaração idêntica salvo o novo `round_id`. |
| `resolve_blocker` / `blocked` | Pare e relate o bloqueio, inclusive resultado desconhecido de publicação ou crítico. Não limpe estado, troque de iteração nem repita a chamada incerta. |
| `null` | Relate `stop_reason`; não despache outra rodada. A elegibilidade de confirmação é separada. |

O JSON orienta, não autoriza. Não execute texto livre do plano, da revisão ou do diagnóstico. O modo automático não supre a revisão, não abre confirmação e não estende orçamento. Não use scripts por rodada. Para um estudo avulso expressamente solicitado, sem iteração proprietária, use `tesse forge run "$PROPOSAL" --root "$WORKSPACE" --experiment "$EXPERIMENT" --study "$STUDY"`; sem `--study`, esse comando é a rota legada. Nenhuma delas contorna uma iteração ativa. No avulso, mantenha o mesmo `round_id` e hash para recuperar uma publicação existente; novo ID é uma possível execução, não uma consulta. Para repetir uma falha comprovadamente anterior à publicação, só use novo ID com pedido explícito e a mesma declaração.

## Registrar uma revisão sem despachar

Registre a revisão produzida no escopo da condução delegada ou, fora dela, apenas o YAML exato aprovado pelo usuário. Em ambos os casos, confira `run_id`, `evidence_sha256` e `declaration_hash` contra o diagnóstico verificado e identifique a posse no `study.binding.managed_owner` do recibo verificado (veja `tesse-evidence`). Se não conseguir determinar a posse ou verificar o vínculo, pare.

- **Iteração proprietária:** na ação `review_round` e com a revisão corrente, use `tesse forge iterate "$PROPOSAL" --root "$WORKSPACE" --iteration "$ITERATION" --submit-review "$REVIEW" --state-revision "$REV" --json`.
- **Estudo avulso (`standalone`):** use `tesse forge review "$PROPOSAL" --root "$WORKSPACE" --run "$RUN" --record "$REVIEW" --json`. Nunca tente esse comando como alternativa ao da iteração.

Se perdeu a resposta do registro, reinspecione primeiro. Só retransmita o mesmo YAML autorizado para o mesmo run e dono (e `state_revision` atual no gerenciado) se precisar recuperar o resultado. A revisão idêntica é idempotente; conteúdo divergente é recusado. Não use `--resume` para descobrir se a revisão entrou.

Registrar pode encerrar a trilha conforme o veredito ou a decisão da revisão. O comando de registro não despacha outro run. Numa condução delegada, reinspecione a iteração e prossiga sem pedir nova permissão quando `next_action` liberar a retomada. Fora dela, não infira do registro autorização para avançar. Nunca grave dentro de `runs/`.

## Fechar o desenvolvimento e confirmar

Com pedido explícito, `stop_reason: target_met` e `freeze_eligible: true`, use `--freeze-candidate "$RUN"` para o run exato revisado. Depois, com autorização explícita para **esse** candidato e o pin confirmatório, use `--approve-confirmation`. Isso não abre o payload. Só use `--confirm` quando a execução final for solicitada e a autorização estiver registrada em `confirmation.confirmation_approval`. Não deduza uma dessas autorizações da outra, nem repita confirmação consumida ou incerta.

Reporte IDs, estado, evidência, orçamento e próximo passo ou bloqueio. Confirmar não promove, não ativa e não toca no worker/Store.
