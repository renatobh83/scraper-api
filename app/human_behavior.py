import asyncio
import random

from playwright.async_api import Page


async def jitter(min_seconds: float = 0.3, max_seconds: float = 1.2):
    """Espera um intervalo aleatório."""
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


async def human_mouse_move(
    page: Page,
    x: float,
    y: float,
):
    """Move o mouse até um ponto em pequenos passos."""
    current = await page.mouse.position

    start_x = current["x"] if current else x
    start_y = current["y"] if current else y

    steps = random.randint(8, 20)

    for i in range(1, steps + 1):
        progress = i / steps

        # Pequena variação para não formar uma linha totalmente perfeita
        offset_x = random.uniform(-2, 2)
        offset_y = random.uniform(-2, 2)

        new_x = start_x + (x - start_x) * progress + offset_x
        new_y = start_y + (y - start_y) * progress + offset_y

        await page.mouse.move(new_x, new_y)
        await asyncio.sleep(random.uniform(0.01, 0.04))


async def human_scroll(
    page: Page,
    min_distance: int = 200,
    max_distance: int = 600,
):
    """Executa alguns scrolls com distância e intervalo variáveis."""

    distance = random.randint(min_distance, max_distance)

    await page.mouse.wheel(
        0,
        distance,
    )

    await jitter(0.4, 1.5)


async def move_and_click(
    page: Page,
    locator,
):
    """Move até o elemento antes de clicar."""

    box = await locator.bounding_box()

    if not box:
        await locator.click()
        return

    # Não mira sempre exatamente no centro
    x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
    y = box["y"] + box["height"] * random.uniform(0.35, 0.65)

    await human_mouse_move(page, x, y)

    await jitter(0.15, 0.5)

    await page.mouse.click(x, y)

    await jitter(0.5, 1.5)
