# DB Uso Itaú

Robô de download e extração dos laudos de avaliação de imóveis do **Itaú**
na plataforma [Inspectos](https://inspectos.com), gravando os dados
estruturados no mesmo banco Postgres já usado para os laudos do Santander
(projeto [Automatiza-o-Uono](https://github.com/rafaelbertochi1/Automatiza-o-Uono)).

Autor: Alessandro Lima Sanchez / Uono Sanchez.

## O que este projeto faz

1. **`itau_downloader.py`** - robô [Playwright](https://playwright.dev/python/)
   que faz login na Inspectos (sessão salva localmente, sem senha em
   texto no código), seleciona o cliente Itaú e baixa em massa os PDFs
   de "Laudo de Avaliação" de um período de datas para `data/laudos/`.
2. **`itau_extractor.py`** - lê cada PDF baixado com `pdfplumber`, extrai
   os dados do imóvel e da avaliação (endereço, área, valores, etc.) e
   grava/atualiza na tabela `laudos_itau` do Postgres.

A tabela `laudos_itau` usa **exatamente as mesmas colunas** da tabela
`laudos` (Santander) do Automatiza-o-Uono - só muda o nome da tabela e a
origem dos dados - para permitir consultas e relatórios unificados entre
os dois bancos depois.

## O que NÃO é gravado nem versionado

- CPF/CNPJ e nome do proponente (cliente) não são extraídos para o banco -
  só dados do imóvel e da avaliação.
- PDFs baixados (`data/laudos/*.pdf`), a sessão de login salva
  (`sessao_inspectos_itau.json`) e os logs de execução (`logs/`) ficam
  só na sua máquina - estão no `.gitignore` e nunca devem ser commitados,
  pois contêm dados confidenciais de clientes (CPF, nome, endereço, fotos).

## Pré-requisitos

- Python 3.10+
- Docker (para o Postgres) - **este projeto reaproveita o mesmo
  container Postgres já usado pelo Automatiza-o-Uono**, não sobe um novo.
  Se ele ainda não estiver rodando, suba-o a partir do
  `docker-compose.yml` daquele outro repositório
  (`Backend l Script Extração Laudos/docker-compose.yml`).
- Uma conta com acesso à Inspectos (cliente Itaú) para o login manual
  na primeira execução do robô.

## Passo a passo

### 1. Clonar o repositório

```bash
git clone https://github.com/rafaelbertochi1/db_use_ita-.git
cd db_use_ita-
```

### 2. Criar o ambiente Python e instalar as dependências

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 3. Garantir que o Postgres compartilhado está rodando

No repositório Automatiza-o-Uono, dentro da pasta
`Backend l Script Extração Laudos`:

```bash
docker compose up -d
```

Isso sobe o container `postgres_pdf` na porta `5432` (usuário/senha
`postgres`/`postgres`, banco `testdb`) - o mesmo banco onde a tabela
`laudos` (Santander) já existe. Este projeto só se conecta nele, via as
mesmas variáveis de ambiente já usadas no outro robô:

| Variável | Padrão      | Descrição                    |
|----------|-------------|-------------------------------|
| `PGURL`  | `127.0.0.1` | Host do Postgres              |
| `PGNAME` | `testdb`    | Nome do banco                 |
| `PGUSR`  | `postgres`  | Usuário                       |
| `PGPASS` | `postgres`  | Senha                         |
| `PGPORT` | `5432`      | Porta                         |

Se o seu Postgres usa outras credenciais, exporte as variáveis antes de
rodar o extractor, por exemplo:

```bash
export PGPASS="sua_senha_aqui"
```

### 4. Baixar os laudos

```bash
python itau_downloader.py
```

Na primeira execução, abra o arquivo e mude `HEADLESS = False` pra fazer
o login manual uma vez (o robô pausa e espera você logar e selecionar o
cliente Itaú, se pedir). Depois disso pode voltar `HEADLESS = True` - a
sessão fica salva em `sessao_inspectos_itau.json` (local, fora do Git).

O robô vai pedir a data inicial e final do período. Teste primeiro com um
período curto antes de rodar um intervalo grande.

> **Atenção:** o fluxo de clique dentro de "baixar um laudo" foi copiado
> do robô que já funciona para o Santander na mesma Inspectos - a
> navegação (login, filtro de período, paginação) é igual, mas o texto
> exato do menu de download pode ser diferente pro Itaú (o laudo dele se
> chama "Laudo de Avaliação", não "Laudo Completo"). Rode a primeira vez
> com `HEADLESS = False` e confira se o clique em "Laudo Completo" em
> `itau_downloader.py` bate com o que aparece na tela; ajuste o texto se
> for diferente.

### 5. Extrair os dados para o banco

```bash
python itau_extractor.py
```

Processa todo PDF em `data/laudos/` que ainda não está na tabela
`laudos_itau`, e grava/atualiza os registros (identificados pelo código
do laudo, ex: `UAK9721`).

### 6. Conferir os dados

Acesse o Adminer (se estiver rodando, a partir do Automatiza-o-Uono) em
`http://localhost:8080`, sistema PostgreSQL, e consulte a tabela
`laudos_itau`.

## Estrutura da tabela `laudos_itau`

Mesmas colunas de `laudos` (Santander): `numero_proposta`,
`codigo_laudo` (chave única), `data_avaliacao`, `endereco`, `numero`,
`complemento`, `tipo_imovel`, `area_privativa_m2`, `area_comum_m2`,
`area_total_m2`, `quartos`, `suites`, `banheiros`, `vagas`,
`idade_anos`, `padrao_acabamento`, `estado_conservacao`,
`valor_mercado`, `valor_venda_forcada` (sempre `0` - o laudo do Itaú não
tem esse conceito), `valor_unitario_m2`, `coordenadas`, `latitude`,
`longitude`, `path`, `modelo_usado` (`fisico` ou `eletronico`).

O parser foi validado com exemplos reais dos dois modelos de laudo do
Itaú (Físico/Casa e Eletrônico/Apartamento). Outras combinações (ex:
Apartamento Físico, Casa Eletrônico, laudo de terreno) podem precisar de
pequenos ajustes nos regex de `itau_extractor.py` - se algum campo vier
vazio ou errado pra um novo PDF, é o primeiro lugar pra olhar.
