# Descoberta de ofertas do Mercado Livre

## Situação da integração

A descoberta geral de ofertas relâmpago está desabilitada. A documentação oficial
consultada confirma recursos de gerenciamento das campanhas `LIGHTNING` de itens
de vendedores convidados, mas não confirma um feed ou endpoint autorizado para
descobrir ofertas relâmpago gerais do marketplace.

Por isso, a ação **Buscar ofertas** exige o worker ativo e apenas enfileira um
ciclo nele. O provedor padrão registra a indisponibilidade, não consulta uma
rota presumida e não cria ofertas. O adaptador está em
[`dashboard/mercadolivre.py`](../dashboard/mercadolivre.py). Um futuro adaptador
deve implementar o contrato `OfferProvider` e somente retornar dados que tenham
origem oficial e autorização verificadas.
Adaptadores devem converter falhas de comunicação ou respostas inválidas em
`OfferProviderError` para que o worker as registre no histórico do sistema.

O formato interno `OfferCandidate` é um contrato do projeto, não uma descrição
do payload de qualquer endpoint do Mercado Livre.

## O que a documentação confirma

Na página oficial [Ofertas relâmpago](https://developers.mercadolivre.com.br/pt_br/ofertas-relampago),
atualizada em 09/06/2026 na consulta, o Mercado Livre informa que vendedores são
convidados periodicamente para participar de promoções. A chamada documentada
para consultar itens de uma campanha é:

```text
GET /seller-promotions/promotions/{PROMOTION_ID}/items?app_version=v2&promotion_type=LIGHTNING
Authorization: Bearer {ACCESS_TOKEN}
```

O exemplo de resposta contém `id`, `start_date`, `finish_date`, `status`,
`price`, `original_price`, `max_discounted_price`, `min_discounted_price` e
`stock`. A página descreve os estados `candidate`, `pending` e `started`, entre
outros. Essa operação consulta itens associados a uma campanha de vendedor; não
é uma busca geral do marketplace. Ela também não fornece, nessa resposta, todos
os dados de produto necessários para publicação, como título, imagem ou URL.

A documentação oficial de [Busca de itens](https://developers.mercadolivre.com.br/pt_br/itens-e-buscas),
consultada em 26/11/2026, descreve pesquisas e consultas de itens, inclusive
recursos por vendedor. Ela não documenta nessas páginas um endpoint geral para
descobrir ofertas relâmpago.

O endereço público [Afiliados do Mercado Livre](https://www.mercadolivre.com.br/afiliados)
redirecionou para autenticação durante a consulta. Assim, os requisitos atuais
de elegibilidade e autorização do programa de afiliados não foram confirmados.
O projeto não gera links afiliados nem divulga produtos por esse programa.

## Cache, limitação e testes

O adaptador futuro usa o cache do Django para armazenar resultados por 60
segundos e limita consultas repetidas do mesmo provedor a uma por 10 segundos.
Esse intervalo é uma proteção local do aplicativo, não um limite oficial da API.
Com o cache padrão em memória, a proteção vale somente dentro de cada processo.
Implantações com múltiplos processos devem configurar um cache compartilhado.

Os testes de provedor usam somente ofertas sintéticas identificadas como dados
de teste. Não são chamadas nem respostas reais do Mercado Livre.
