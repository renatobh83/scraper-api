# News Scraper API

Esboço inicial de uma API Python para coleta de notícias com **Playwright** como núcleo. O Chromium é iniciado sob demanda na primeira chamada a `/news` e permanece disponível enquanto a API estiver ativa, mas apenas uma página é criada por scraping e fechada ao final. Por padrão, o navegador roda em modo headless.

## Arquitetura inicial

```text
Cliente HTTP
    |
    v
FastAPI (/health, /sources, /news)
    |
    v
Inicialização sob demanda + semáforo de concorrência + cache curto
    |
    v
BrowserManager
    |
    v
Chromium único -> página temporária -> HTML renderizado -> BeautifulSoup -> NewsItem
```

A aplicação não inicia um Chromium novo a cada requisição. Ela cria uma instância única sob demanda, limita o número de scrapes simultâneos a um por padrão e fecha cada página logo após a extração. Imagens, mídia e fontes são bloqueadas porque não são necessárias para extrair manchetes. Scripts, XHR, Fetch e documentos continuam permitidos para que páginas dinâmicas funcionem.

## Estrutura

```text
news-scraper-api/
├── app/
│   ├── main.py
│   ├── browser.py
│   ├── config.py
│   ├── models.py
│   └── scrapers/
│       ├── base.py
│       └── investing.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .dockerignore
└── seccomp_profile.json
```

## Executar com Docker

O `Dockerfile` voltou a usar a imagem oficial do Playwright `mcr.microsoft.com/playwright/python:v1.61.0-noble`, com o pacote Python fixado na mesma versão. O container usa um único worker, `ipc: host`, usuário não-root `pwuser` e o perfil `seccomp_profile.json` versionado para o Chromium [1].

Copie o arquivo de ambiente e suba o serviço:

```bash
cp .env.example .env
docker compose up -d --build
```

Verifique a API:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/sources
curl 'http://localhost:8000/news?source=investing&limit=10'
```

A documentação interativa fica disponível em `http://localhost:8000/docs`.

Para acompanhar logs:

```bash
docker compose logs -f news-api
```

Para parar:

```bash
docker compose down
```

## Debug local com navegador visível

Em uma máquina com interface gráfica, copie `.env.example` para `.env` e altere:

```env
DEBUG_BROWSER=1
DEBUG_PAUSE=1
```

Com `DEBUG_BROWSER=1`, o Chromium é iniciado com `headless=False` e `slow_mo`. Com `DEBUG_PAUSE=1`, o scraper pausa em `page.pause()` antes de fechar a página, permitindo inspeção interativa. Isso deve ser usado localmente; um container comum em VPS não possui display gráfico. Para a VPS, mantenha:

```env
DEBUG_BROWSER=0
DEBUG_PAUSE=0
```

## Configurações de baixo consumo

| Variável | Padrão | Função |
|---|---:|---|
| `MAX_CONCURRENT_SCRAPES` | `1` | Limita navegações Playwright simultâneas |
| `CACHE_TTL_SECONDS` | `120` | Evita repetir a mesma consulta em curto intervalo |
| `BLOCKED_COOLDOWN_SECONDS` | `60` | Aguarda após um 403 antes de tentar novamente |
| `MAX_NEWS_ITEMS` | `20` | Impede respostas excessivamente grandes |
| `NAVIGATION_TIMEOUT_MS` | `30000` | Limita a navegação da fonte |
| `SELECTOR_TIMEOUT_MS` | `10000` | Limita a espera por elementos |
| `DEBUG_BROWSER` | `0` | Abre ou não a janela do Chromium |
| `DEBUG_PAUSE` | `0` | Pausa ou não a página para inspeção |

Não aumente os workers do Uvicorn enquanto o scraper for o núcleo da aplicação. Cada worker criaria seu próprio processo e sua própria instância do Chromium. A primeira versão usa um único worker para manter previsível o consumo da VPS.

## Sobre o scraper da Investing

O primeiro adaptador navega para `https://br.investing.com/news/headlines`, espera o DOM dinâmico e analisa o HTML renderizado. Para cada notícia, ele extrai os ativos relacionados a partir de `div.news-headlines-list-item.related-pairs a[href*="/equities/"]`. O texto do `span` com `title` é normalizado e retornado somente como símbolo. Por exemplo:

```json
{
  "symbols": ["LMT", "LCO"]
}
```

Os seletores possuem fallbacks, mas a estrutura do site pode mudar. Se o endpoint retornar erro 502 ou zero itens, os logs agora mostram o status HTTP, a URL final, o título da página e uma amostra do texto visível para distinguir bloqueio, challenge e mudança de seletor. Além disso, são salvos screenshot, HTML e metadados em `/tmp/news-debug`.

Para copiar esses arquivos para a VPS após uma falha:

```bash
mkdir -p ./debug

docker cp news-scraper-api:/tmp/news-debug/. ./debug/
ls -lh ./debug
```

## Proxy HTTP opcional

O `BrowserManager` aceita um proxy HTTP autenticado. Para usar a conexão completa:

```env
PROXY_URL=http://USUARIO:SENHA@HOST:PORTA
```

Ou use as variáveis separadas, que são mais seguras quando a senha contém caracteres especiais:

```env
PROXY_SERVER=http://HOST:PORTA
PROXY_USERNAME=USUARIO
PROXY_PASSWORD=SENHA
```

`PROXY_URL` tem prioridade se os dois formatos forem preenchidos. Antes de testar a Investing, valide o próprio proxy com `curl`:

```bash
curl -v -x "http://USUARIO:SENHA@HOST:PORTA" https://api.ipify.org
```

O proxy deve aceitar o método `CONNECT` para destinos HTTPS. Não coloque a senha diretamente no código nem publique o arquivo `.env`.

O ajuste do parser deverá ser feito somente em `app/scrapers/investing.py`.

Playwright reduz o risco de bloqueio que ocorreu com uma requisição HTTP simples, mas não garante que todo bloqueio desapareça. CAPTCHA, firewall, reputação do IP, cookies, frequência de navegação e políticas da fonte ainda podem impedir o acesso. A aplicação evita reloads automáticos, limita a concorrência e não tenta contornar CAPTCHA ou outras proteções; nesses casos, devemos avaliar uma fonte oficial, RSS ou uma estratégia permitida pela fonte.

## Próximas evoluções

A API usa cache por 120 segundos por padrão. Assim, recarregar o endpoint não navega novamente na Investing durante esse intervalo. Use `GET /news?refresh=true` somente quando quiser forçar uma consulta nova. Se uma consulta posterior for bloqueada, a API devolve o último resultado válido com `stale: true`, quando existir, em vez de perder imediatamente os dados já coletados. Após um 403, o serviço aguarda 60 segundos antes de uma nova tentativa para não intensificar o bloqueio.

A ordem recomendada é validar `/health`, testar `/news`, confirmar os seletores da fonte e só depois adicionar persistência. O cache atual é apenas em memória e serve para o esboço. Quando houver agendamento, retries, várias fontes ou tarefas demoradas, será melhor separar API e worker e colocar o estado em Redis ou banco.

## Referências técnicas

[1]: https://playwright.dev/python/docs/docker "Playwright Python — Docker"
[2]: https://playwright.dev/python/docs/api/class-browser#browser-new-context "Playwright Python — Browser.new_context"
[3]: https://playwright.dev/python/docs/api/class-route "Playwright Python — Route"
