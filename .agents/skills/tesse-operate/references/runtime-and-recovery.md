# Estado, autenticação e recuperação

## Autoridade do estado

SQLite versiona perfis, ativações, intenções, sessões, estudos e eventos. O perfil ativo no banco é a autoridade; `bot.yml` é projetado de forma atômica, com detecção de concorrência. Não edite o banco ou force flags para contornar um bloqueio.

Há uma ordem global por vez. A sessão usa a banca nativa confirmada; a primeira stake é a fração configurada (padrão 5%), inteira e arredondada para baixo. Após WIN, reinveste stake e lucro confirmado. Termina na primeira LOSS ou segunda WIN; devolução mantém a etapa. Não use a banca simulada para financiar ordens.

Uma perda financeira e uma falha técnica são eventos diferentes. Verifique logs/eventos e identidade da intenção antes de concluir que uma LOSS é defeito do programa.

## Consulta e preparação

Localize `bot.yml`, diretório de dados e `state.sqlite` antes de executar. Com estado existente:

```text
tesse --config "bot.yml" status
tesse --config "bot.yml" versions
```

Esses comandos podem inicializar Store/baseline; não os descreva como inspeção universal sem escrita. `report` grava JSON em `reports/daily-<dia>.json`, sob o diretório de dados. `configure` pode recuperar a projeção, e `configure --set seção.chave=valor` altera configuração. Use o comando suportado para mudanças solicitadas e respeite sessões/conflitos informados.

`init` cria configuração, banco e chave quando ausentes; é idempotente, mas não restaura dados perdidos. Execute-o para preparar uma instalação solicitada, nunca para eliminar inconsistências de uma conta existente.

## Cofre e identidade da conta

O SDK mantém sessão/journal cifrados em `data/private/sdk` e chave em `data/private/sdk.key`, relativos ao diretório de dados real. O cofre tem posse exclusiva por processo: `login` e `inventory` exigem worker parado.

Use o login interativo com e-mail/senha ocultos e 2FA quando aplicável. Preserve autenticação/endpoints existentes. Não copie o SDK, adicione segredos ao projeto ou leia conteúdo privado para incluí-lo em relatórios.

Se chave faltar com cofre/journal existentes, restaure a chave original de backup protegido. Não gere substituta. Chave inválida, ACL inadequada e limite de journal de 4 MiB exigem diagnóstico e manutenção que preservem as ordens.

Uma conta autenticada diferente não deve compartilhar histórico ou Soros com a anterior. Resolva a identidade da conta; não altere o binding para passar no teste.

## Ordem pendente e parada

`DISPATCHING`, `ACCEPTED` e `UNKNOWN` são pendências. Uma exceção de envio pode ocorrer após o broker aceitar a ordem; não assuma `NOT_SENT` nem republique uma intenção.

O worker consulta o journal e reconcilia pela identidade original ao reiniciar. Restaure primeiro acesso ao estado/cofre correto. Com esse acesso restabelecido, um reinício autorizado pode ser a própria etapa de reconciliação; não é necessário inventar um resultado para permitir a consulta. Resultado ainda desconhecido mantém novas entradas bloqueadas; não force `settle`, apague journals ou crie outra intenção para a mesma entrada.

`stop --wait 180` pede parada e espera por até 180 segundos; o worker também acompanha a pendente por até 180 segundos. Consulte novamente se o comando retornar com processo ativo. Uma pendência inconclusiva continua durável e bloqueada para o reinício.

`start --observe-only` desabilita envios também no SDK e não dispara revisão/calibração automática. Ainda conecta à conta e mantém estado; não o apresente como modo offline.

## Troca de perfil

`versions` mostra o ativo. `rollback` exige worker parado, sessão Soros encerrada e nenhuma ordem pendente. Parar o processo não encerra automaticamente uma sessão esperando outro sinal. Não force sinais, feche sessões artificialmente ou altere flags para permitir rollback.

Mudanças entram nas fronteiras de sessão conforme o fluxo do programa. Quando o pedido não puder ser concluído com o estado atual, informe a pendência concreta e o que permitirá continuar.

Fontes: `tesse/{cli,state,versioning,execution,worker,sdk}.py`.
