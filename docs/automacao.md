# Motor de automação

## Execução

O agendador roda em um processo Django separado do servidor web e não depende
de uma página aberta. Depois de instalar os requisitos e aplicar as migrações,
execute em um terminal dedicado:

```powershell
.\.venv\Scripts\python.exe manage.py run_automation
```

Inicie o servidor web em outro terminal. O painel só aceita iniciar ou retomar
o motor enquanto o worker estiver conectado. O processo atualiza um heartbeat
persistido; se ele parar, o painel informa que está desconectado.

O intervalo inicial é de 10 minutos. Um ciclo é agendado por intervalo, com
coalescência de atrasos e no máximo uma execução simultânea. Pausar ou parar
remove os ciclos futuros. Retomar inicia um novo intervalo, sem executar
retrospectivamente ciclos perdidos. Um bloqueio de processo impede dois workers
simultâneos para o mesmo banco. No encerramento normal, o APScheduler aguarda a
conclusão do ciclo em andamento.

Os registros de mensagens distinguem `Aguardando`, `Preparada`, `Enviada`,
`Falhou` e `Cancelada`. Preparar uma mensagem nunca é registrado como envio.
Uma pausa durante a busca interrompe o ciclo antes de armazenar e divulgar uma
oferta; uma solicitação de busca manual é enfileirada no próprio worker.

## Limitações de integração

O provedor padrão de descoberta permanece desabilitado: a documentação
consultada confirma campanhas de ofertas relâmpago de vendedores convidados,
não uma fonte geral autorizada para o marketplace. O worker registra a
indisponibilidade e não cria ofertas sintéticas.

O provedor padrão de WhatsApp também não está autorizado nem conectado. Ele não
envia para o número pessoal ou para o grupo existente. Com um provedor de
descoberta injetado, o motor só considera ofertas disponíveis, dentro do prazo,
com preços positivos confirmados na consulta e link de afiliado marcado como
válido. Na ausência de um provedor autorizado, conectado e compatível com o
destino, a mensagem pode ser preparada e fica nesse estado; nenhum disparo
automático ocorre.

Na opção `Preparação local`, mensagens geradas permanecem no estado
`Preparada` e não são tratadas como enviadas. A opção `Desativado` impede a
preparação automática; sem provedor de envio compatível, disparos automáticos
permanecem bloqueados.

Uma mensagem já aceita por um provedor não pode ser desfeita por uma pausa ou
parada posterior. O motor espera a operação em andamento terminar no
encerramento normal. A próxima execução fica em memória no APScheduler e é
recalculada quando o worker reinicia; não há recuperação de ciclos atrasados.
Produtos com envio aguardando, preparado, enviado ou falho não são tentados de
novo automaticamente. Essa política evita duplicações quando o resultado de
uma chamada externa for incerto; uma falha exige revisão manual antes de uma
nova tentativa.
