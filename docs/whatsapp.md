# Divulgação de ofertas pelo WhatsApp

## Pesquisa oficial da Meta

Consulta realizada em 26/11/2026:

- [WhatsApp Groups API](https://developers.facebook.com/documentation/business-messaging/whatsapp/groups)
- [Primeiros passos da Groups API](https://developers.facebook.com/documentation/business-messaging/whatsapp/groups/get-started)
- [Referência de gerenciamento de grupos](https://developers.facebook.com/documentation/business-messaging/whatsapp/groups/reference)
- [Mensagens em grupos](https://developers.facebook.com/documentation/business-messaging/whatsapp/groups/groups-messaging)
- [Contas Oficiais de Empresas (OBA)](https://developers.facebook.com/documentation/business-messaging/whatsapp/official-business-accounts)
- [Números de telefone empresariais](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/phone-numbers)
- [Envio e janela de atendimento](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages)
- [Opt-in para WhatsApp](https://developers.facebook.com/documentation/business-messaging/whatsapp/getting-opt-in)
- [Política de Mensagens do WhatsApp Business](https://whatsappbusiness.com/pt-br/policy/)

## Compatibilidade e elegibilidade

A Groups API documentada pela Meta cria novos grupos pela Cloud API e gera
links de convite; os participantes entram voluntariamente. O endpoint de envio
usa o ID de um grupo criado pela própria API. A documentação não descreve uma
forma de importar ou conectar um grupo existente do WhatsApp pessoal. Portanto,
este projeto não considera o grupo público existente compatível e não tenta
enviá-lo.

Para usar grupos pela API, é necessário um número empresarial registrado na
Cloud API, uma conta **Official Business Account (OBA)**, webhooks de grupos,
assinatura da conta de mensagens e permissão `whatsapp_business_messaging`.
OBA exige, entre outros critérios, portfólio empresarial verificado, número na
Plataforma há pelo menos 30 dias, verificação em duas etapas e nome de exibição
aprovado. A elegibilidade desta conta não foi verificada.

O limite documentado é de **8 participantes por grupo** e até **10.000 grupos
por número empresarial**; somente uma empresa Cloud API pode participar de
cada grupo. Grupos são por convite. Mensagens de texto são suportadas, mas
mensagens interativas, chamadas, mensagens temporárias, visualização única e
outros tipos não são.

Números do WhatsApp Business App não podem usar a Groups API. A Meta documenta
uma opção de coexistência do Business App com Cloud API via Embedded Signup,
mas chats de grupo não são sincronizados e a integração exige um Solution
Partner ou Tech Provider. Isso não habilita o grupo pessoal já existente. Um
número pessoal também não é um número empresarial registrado na Cloud API.
Números já em uso no WhatsApp não podem ser registrados na Cloud API sem antes
remover a conta existente; a coexistência documentada é para contas do Business
App e continua sem suporte a grupos existentes.

Mensagens promocionais automatizadas devem respeitar a política da Meta,
incluindo consentimento (opt-in) apropriado, modelos aprovados quando exigidos,
preferências de marketing, pedidos de descadastro, qualidade e limites da conta.
Uma resposta livre via API só é permitida dentro da janela de atendimento
iniciada por mensagem ou chamada do usuário; fora dela, use os modelos
aprovados aplicáveis. Essas regras não autorizam automação via WhatsApp Web.

## Comportamento implementado

- `dashboard/whatsapp.py` separa o provedor de preparação do contrato de envio.
- A preparação usa ofertas e links afiliados já cadastrados, formata os valores
  em reais e salva mensagem, destino identificado, data, status e falhas.
- A deduplicação impede múltiplos registros da mesma mensagem preparada para a
  mesma oferta e destino; a marcação de teste é separada.
- O destino armazenado é apenas um rótulo local do grupo. Links de convite não
  são aceitos como destino nem verificados.
- O provedor de envio oficial falha de forma explícita. Nenhuma sessão,
  credencial, cookie, automação do WhatsApp Web ou requisição de envio é usada.
- O painel permite copiar a mensagem para compartilhamento manual; o histórico
  distingue mensagens preparadas, enviadas por um provedor real e falhas. Com o
  provedor atual, não há envios reais nem sucesso fictício.

Para este cenário, a alternativa é revisar e copiar a mensagem e publicá-la
manualmente no grupo, seguindo as regras do grupo e do WhatsApp. Um futuro uso
da Groups API exigiria uma nova configuração com conta OBA elegível, número na
Cloud API e grupo criado por essa API; não se deve reutilizar o grupo existente
sem suporte explícito e verificação oficial.
