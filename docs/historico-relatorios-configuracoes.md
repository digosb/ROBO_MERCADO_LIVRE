# Histórico, relatórios e configurações

## Histórico

O histórico lista os registros reais de `Envio`, com filtros por data, nome do
produto, estado do envio e tipo de oferta. Registros novos preservam o nome do
produto, preço, desconto e link de afiliado no momento da divulgação; registros
anteriores à inclusão desses snapshots podem não ter todos esses valores e usam
os dados atuais da oferta como alternativa quando disponíveis.

Os eventos operacionais são exibidos separadamente do histórico de envios. Uma
mensagem preparada não é contabilizada como enviada.

## Indicadores

Os relatórios consultam os registros persistidos:

- **Ofertas encontradas:** ofertas cadastradas.
- **Ofertas aprovadas:** ofertas atualmente marcadas como disponíveis.
- **Links válidos:** links de afiliado com validação marcada como válida.
- **Mensagens enviadas:** envios confirmados pelo provedor.
- **Falhas:** envios com falha mais eventos do sistema com nível erro ou crítico.
  Eventos de envio são excluídos dessa segunda parcela para não contar uma falha
  duas vezes.
- **Percentual de sucesso:** envios confirmados divididos pela soma de envios
  confirmados e envios com falha. Preparações, cancelamentos e tentativas ainda
  pendentes ficam fora do denominador; sem tentativas concluídas, o resultado é
  `0,00%`.

## Configurações

O backend valida o intervalo de 1 a 1440 minutos, o limite diário de 1 a 10000
envios, o destino e os provedores. A restrição de horário usa o fuso configurado
no Django (`America/Sao_Paulo` neste projeto), aceita intervalos noturnos e não
aceita horários inicial e final iguais.

As notificações disponíveis são avisos internos do painel, derivados dos logs
persistidos; não enviam mensagens externas. As opções de provedores refletem
somente as integrações existentes: busca geral do Mercado Livre e geração
automatizada de links de afiliado permanecem indisponíveis, e o WhatsApp fica em
preparação local. A seleção de envio pela API oficial é rejeitada enquanto não
houver um provedor autorizado, conectado e compatível com o destino.
