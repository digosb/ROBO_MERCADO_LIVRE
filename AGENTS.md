# Instruções do projeto

## Idioma

- Responder sempre em português brasileiro claro e natural.
- Preservar termos técnicos em inglês somente quando forem nomes de APIs, bibliotecas, comandos, tipos ou siglas já estabelecidas; explicar termos ambíguos em português.
- Usar nomes de arquivos, comandos e trechos de código exatamente como aparecem no projeto.

## Projeto

- Aplicação web em Python com Django Templates; interface em HTML, CSS e JavaScript sem dependências de front-end.
- Instalar dependências: `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
- Preparar banco: `.\.venv\Scripts\python.exe manage.py migrate`
- Iniciar localmente: `.\.venv\Scripts\python.exe manage.py runserver`
- Executar testes: `.\.venv\Scripts\python.exe manage.py test`
- Executar verificações Django: `.\.venv\Scripts\python.exe manage.py check`
- O domínio usa `Produto`, `Oferta`, `LinkAfiliado`, `Envio`, `Configuracao` e `LogSistema`; aplicar alterações de schema com `makemigrations` e `migrate`.
- Produtos são únicos por identificador externo e URL original; apenas um envio pendente ou concluído pode existir para cada oferta.
- Não inserir produtos, ofertas, links ou envios de exemplo automaticamente; não apresentar registros simulados como dados reais.
- As ações de busca e WhatsApp devem informar explicitamente quando forem apenas simulações, sem integração externa.
