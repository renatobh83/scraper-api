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

O `Dockerfile` usa a imagem oficial do Playwright com os browsers e dependências do sistema. O pacote Python `playwright` permanece fixado na mesma versão da imagem. O container executa como `pwuser`, usa `--init`, `ipc: host` e o arquivo `seccomp_profile.json` oficial para permitir o sandbox do Chromium sem executar o navegador como root [1].

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
| `CACHE_TTL_SECONDS` | `30` | Evita repetir a mesma consulta em curto intervalo |
| `MAX_NEWS_ITEMS` | `20` | Impede respostas excessivamente grandes |
| `REQUEST_TIMEOUT` | `30` | Limita a navegação da fonte |
| `DEBUG_BROWSER` | `0` | Abre ou não a janela do Chromium |
| `DEBUG_PAUSE` | `0` | Pausa ou não a página para inspeção |

Não aumente os workers do Uvicorn enquanto o scraper for o núcleo da aplicação. Cada worker criaria seu próprio processo e sua própria instância do Chromium. A primeira versão usa um único worker para manter previsível o consumo da VPS.

## Sobre o scraper da Investing

O primeiro adaptador navega para `https://br.investing.com/news/headlines`, espera o DOM dinâmico e analisa o HTML renderizado. Os seletores possuem fallbacks, mas a estrutura do site pode mudar. Se o endpoint retornar erro 502 ou zero itens, o próximo passo será capturar a estrutura real entregue à sua região/IP e ajustar somente `app/scrapers/investing.py`.

Playwright reduz o risco de bloqueio que ocorreu com uma requisição HTTP simples, mas não garante que todo bloqueio desapareça. CAPTCHA, firewall, reputação do IP, cookies e políticas da fonte ainda podem impedir o acesso. A aplicação não tenta contornar CAPTCHA; nesses casos, devemos avaliar uma fonte oficial, RSS ou uma estratégia permitida pela fonte.

## Próximas evoluções

A ordem recomendada é validar `/health`, testar `/news`, confirmar os seletores da fonte e só depois adicionar persistência. O cache atual é apenas em memória e serve para o esboço. Quando houver agendamento, retries, várias fontes ou tarefas demoradas, será melhor separar API e worker e colocar o estado em Redis ou banco.

## Referências técnicas

[1]: https://playwright.dev/python/docs/docker "Playwright Python — Docker"
[2]: https://playwright.dev/python/docs/api/class-browser#browser-new-context "Playwright Python — Browser.new_context"
[3]: https://playwright.dev/python/docs/api/class-route "Playwright Python — Route"
