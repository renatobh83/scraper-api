from bs4 import BeautifulSoup


HTML = """
<div data-test="breaking-news">
  <div class="flex flex-col">
    <div data-test="breaking-news-article">
      <a href="/news/stock-market-news/morgan-stanley-prevê-aumento-93CH-2050161">
        Morgan Stanley prevê aumento expressivo de dividendos
      </a>
      <div class="news-headlines-list-item related-pairs">
        <a href="https://br.investing.com/equities/lockheed-martin">
          <span title="Lockheed Martin Corporation">LMT</span>
        </a>
        <a href="https://br.investing.com/commodities/brent-oil">
          <span title="Brent Oil">LCO</span>
        </a>
      </div>
      <div class="flex gap-x-2 text-xs leading-4"></div>
    </div>
    <div data-test="breaking-news-article">
      <a href="/news/stock-market-news/outra-noticia-93CH-2050162">
        Segunda notícia
      </a>
    </div>
  </div>
</div>
"""


def test_breaking_news_selector() -> None:
    soup = BeautifulSoup(HTML, "html.parser")
    container = soup.select_one('div[data-test="breaking-news"]')
    cards = container.select('div[data-test="breaking-news-article"]')

    assert len(cards) == 2
    first = cards[0].select_one('a[href*="/news/"]')
    assert first is not None
    assert first.get_text(" ", strip=True) == (
        "Morgan Stanley prevê aumento expressivo de dividendos"
    )
    assert first["href"].startswith("/news/")

    ticker_links = cards[0].select(
        'div.news-headlines-list-item.related-pairs '
        'a[href*="/equities/"], '
        'div.news-headlines-list-item.related-pairs '
        'a[href*="/commodities/"]'
    )
    assert len(ticker_links) == 2
    ticker_symbols = []
    for link in ticker_links:
        ticker = link.select_one("span[title]")
        if ticker is not None:
            ticker_symbols.append(ticker.get_text(strip=True).upper())

    assert ticker_symbols == ["LMT", "LCO"]


if __name__ == "__main__":
    test_breaking_news_selector()
    print("breaking_news_selector=ok")
