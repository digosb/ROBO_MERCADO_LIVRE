# Links do programa de afiliados do Mercado Livre

## Pesquisa e conclusão

Pesquisa realizada em 26/11/2026. A busca pública encontrou páginas oficiais
de Afiliados e Criadores e do Portal do Afiliado. As páginas específicas de
primeiros passos e geração de links, assim como a documentação pública de
desenvolvedores, não puderam ser abertas nesta sessão (HTTP 403 ou exigência
de autenticação). Não foi possível confirmar por leitura direta quais controles
de geração estão habilitados para esta conta.

Não foi localizado na documentação pública acessível um endpoint oficial de
geração automatizada em massa. Isso não prova que inexista uma API privada ou
recurso exclusivo de determinada conta; nada desse tipo foi confirmado e,
portanto, não foi integrado. Verifique no Portal do Afiliado da própria conta
quais opções estão autorizadas antes de usar este painel.

Fontes oficiais consultadas:

- [Afiliados e Criadores](https://www.mercadolivre.com.br/afiliados)
- [Portal do Afiliado](https://www.mercadolivre.com.br/afiliados-portal-do-afiliado)
- [Afiliados — primeiros passos](https://www.mercadolivre.com.br/afiliados-primeiros-passos)
- [Afiliados — gere seus links](https://www.mercadolivre.com.br/afiliados-gere-seus-links)
- [Documentação de desenvolvedores](https://developers.mercadolivre.com.br/pt_br/)

## O que o painel implementa

O painel oferece importação manual: o usuário gera o link em um mecanismo
oficial disponível para sua conta e informa a URL original do produto, o link
resultante e, opcionalmente, a data em que foi gerado. A URL original precisa
corresponder exatamente a um produto já cadastrado. URLs podem ter até 2048
caracteres.

O serviço `dashboard/afiliados.py` valida que ambas as URLs usem HTTP ou HTTPS,
associa o link ao produto, evita duplicatas por produto e URL afiliada e registra
sucesso ou rejeição em `LogSistema`. A data de geração é opcional para não
confundi-la com a data de importação (`registered_at`).

Uma validação estrutural da URL não prova que o link atribui comissão à conta.
Por isso, links importados começam com status **Pendente**. O sistema não
reescreve URLs, não inventa parâmetros, não segue redirecionamentos para inferir
afiliação e não declara um link válido automaticamente.

## Credenciais

O fluxo manual não requer credenciais no painel. Nenhuma senha, cookie, token ou
segredo da conta é solicitado ou armazenado; o usuário deve acessar diretamente
o site ou aplicativo oficial para gerar cada link. Não foi criado campo de
credenciais porque não há uma API oficial de geração confirmada para configurar.
Se uma integração oficial vier a ser disponibilizada para esta conta, credenciais
devem ser configuradas fora do banco de dados, por variáveis de ambiente ou um
gerenciador de segredos, após confirmar os escopos e as regras aplicáveis.

Os testes usam exclusivamente URLs de exemplo sintéticas e não acessam o
Mercado Livre.
