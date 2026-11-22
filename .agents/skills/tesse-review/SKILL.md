---
name: tesse-review
description: Use when conducting or coordinating an independent review of Tesse repository code, tests, OpenSpec artifacts, or documentation, including Forge implementation. Not for interpreting Forge run evidence, experimental critique, or implementing fixes.
---

# Revisão independente do repositório

Revisar não autoriza implementar. O coordenador delimita o trabalho, consolida os pareceres e espera um handoff explícito antes de qualquer edição.

## Antes de despachar

- Registre para cada revisor: nome da frente, revisão/base a ler, paths incluídos e excluídos, interfaces de outras frentes e alvo de verificação. Resolva sobreposições antes do despacho. A leitura de uma interface compartilhada não transfere sua titularidade; encaminhe o achado ao coordenador com o path do responsável.
- Use uma sessão com acesso somente leitura ou entregue ao revisor um pacote de evidência sem ferramentas de escrita/execução. Um prompt não substitui isolamento. Se nenhum modo puder ser aplicado, não despache como revisor independente.
- O revisor nunca altera código, testes, specs, docs, checkboxes, staging nem histórico. Não acesse segredos, ambiente operacional ou corretora. Se a verificação criar estado ou precisar de execução indisponível, entregue o comando ao coordenador e classifique a observação como não reproduzida.

## Parecer para o coordenador

Para cada achado, envie **severidade · `path:linha` · observado/esperado · impacto · confiança · comando, resultado e revisão (ou motivo de não execução) · lacuna de teste e próximo passo**. Identifique o que foi reproduzido, o que é risco não verificado e o que é lacuna de teste. Liste áreas não revisadas; não trate falta de evidência como aprovação.

Exemplo: **Alta · `tesse/runtime/activation.py:42` · o artefato hipotético seleciona um manifesto divergente, mas o Store deveria vencer · pode selecionar modelo incorreto · média · teste offline de divergência proposto, não executado · adicionar regressão para Store e manifesto conflitantes**.

## Handoff

O coordenador consolida achados, controvérsias e exclusões. Somente um novo handoff explícito para uma mudança OpenSpec aprovada pode iniciar implementação; ele identifica tarefas e paths permitidos, exclusões e verificações. Um fix pequeno, prazo curto ou parecer favorável não substitui o handoff.

Para interpretar evidência de uma rodada, use `tesse-evidence`; para conduzir o ciclo, use `tesse-iterate`. Críticos experimentais do Forge não são revisores do repositório: cada um lê o mesmo pacote de desenvolvimento e emite um parecer inicial isolado, sem editar nem executar. Eles não repartem a evidência por paths nem ganham autoridade de escrita. Consulte [Autoridade de runtime](../../../docs/runtime-authority.md) para o limite operacional.
