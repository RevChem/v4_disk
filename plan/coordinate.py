"""
Сопоставление полей с ближайшими населёнными пунктами через Nominatim (OpenStreetMap).
Запросы выполняются асинхронно с соблюдением лимита 1 req/s (политика Nominatim).

Использование: python find_settlements.py поля.xlsx
Зависимости: pip install pandas aiohttp openpyxl
"""

import sys
import asyncio
import time
import pandas as pd
import aiohttp

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
HEADERS = {"User-Agent": "fields-settlement-matcher/1.0"}
RATE_LIMIT = 1.0  # секунд между запросами (политика Nominatim)


def parse_coords(coord_str: str):
    try:
        parts = [p.strip() for p in str(coord_str).split(",")]
        return float(parts[0]), float(parts[1])
    except Exception:
        return None, None


def _extract_settlement(addr: dict) -> tuple:
    """Извлекает населённый пункт и тип из словаря address."""
    settlement = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("hamlet")
        or addr.get("isolated_dwelling")
        or addr.get("allotments")
        or addr.get("farm")
        or addr.get("suburb")
        or addr.get("quarter")
        or addr.get("municipality")   # широкий fallback
    )
    return settlement


async def _nominatim_request(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    rate_lock: asyncio.Lock,
    last_request_time: list,
    lat: float,
    lon: float,
    zoom: int,
) -> dict | None:
    """Один запрос к Nominatim с заданным zoom. Возвращает data или None при ошибке."""
    params = {
        "lat": lat,
        "lon": lon,
        "format": "jsonv2",
        "addressdetails": 1,
        "zoom": zoom,
        "accept-language": "ru",
    }
    async with semaphore:
        async with rate_lock:
            now = time.monotonic()
            wait = RATE_LIMIT - (now - last_request_time[0])
            if wait > 0:
                await asyncio.sleep(wait)
            last_request_time[0] = time.monotonic()

        try:
            async with session.get(
                NOMINATIM_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except Exception:
            return None


async def get_nearest_settlement(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    rate_lock: asyncio.Lock,
    last_request_time: list,
    idx: int,
    name: str,
    lat: float,
    lon: float,
    total: int,
) -> dict:
    # Первый запрос: zoom=14 — уровень деревни/посёлка
    data = await _nominatim_request(session, semaphore, rate_lock, last_request_time, lat, lon, zoom=14)
    if data is None:
        return {"idx": idx, "settlement": None, "type": None,
                "district": None, "region": None, "error": "Ошибка запроса"}

    addr = data.get("address", {})
    settlement = _extract_settlement(addr)

    # Fallback: zoom=10 — уровень волости/сельсовета, если н.п. не найден
    if not settlement:
        data2 = await _nominatim_request(session, semaphore, rate_lock, last_request_time, lat, lon, zoom=10)
        if data2:
            addr2 = data2.get("address", {})
            settlement = _extract_settlement(addr2)
            if settlement:
                data = data2
                addr = addr2

    place_type = data.get("type") or data.get("category")
    district   = addr.get("county") or addr.get("municipality") or addr.get("district")
    region     = addr.get("state")  or addr.get("region")

    print(f"[{idx + 1}/{total}] {name} → {settlement} ({district})")

    return {
        "idx": idx,
        "settlement": settlement,
        "type": place_type,
        "district": district,
        "region": region,
        "error": None,
    }


async def process(df: pd.DataFrame, coord_col: str) -> list:
    total = len(df)
    # Semaphore(1) — Nominatim требует строго не более 1 одновременного запроса.
    # При наличии собственного сервера Nominatim можно поднять до нужного числа.
    semaphore = asyncio.Semaphore(1)
    rate_lock = asyncio.Lock()
    last_request_time = [0.0]

    tasks = []
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        for idx, row in df.iterrows():
            lat, lon = parse_coords(row[coord_col])
            name = str(row.get("Имя", idx))

            if lat is None:
                async def _bad(i=idx):
                    return {
                        "idx": i, "settlement": None, "type": None,
                        "district": None, "region": None,
                        "error": "Не удалось разобрать координаты",
                    }
                tasks.append(_bad())
            else:
                tasks.append(
                    get_nearest_settlement(
                        session, semaphore, rate_lock, last_request_time,
                        idx, name, lat, lon, total,
                    )
                )

        results = await asyncio.gather(*tasks)

    return sorted(results, key=lambda r: r["idx"])


def main(input_file: str):
    df = pd.read_excel(input_file)

    coord_col = "Координаты центра поля, Ш, Д"
    if coord_col not in df.columns:
        candidates = [c for c in df.columns if "коорд" in c.lower() or "ш," in c.lower()]
        if not candidates:
            print("Не найдена колонка с координатами. Колонки:", df.columns.tolist())
            sys.exit(1)
        coord_col = candidates[0]
        print(f"Используется колонка: {coord_col}")

    results = asyncio.run(process(df, coord_col))

    df["Ближайший н.п."] = [r["settlement"] for r in results]
    df["Тип н.п."]       = [r["type"]       for r in results]
    df["Район"]          = [r["district"]   for r in results]
    df["Регион"]         = [r["region"]     for r in results]
    df["Ошибка"]         = [r["error"]      for r in results]

    output_file = input_file.replace(".xlsx", "_с_нп.xlsx")
    if output_file == input_file:
        output_file = input_file + "_с_нп.xlsx"
    df.to_excel(output_file, index=False)
    print(f"\nГотово! Результат сохранён в: {output_file}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python find_settlements.py <путь_к_файлу.xlsx>")
        sys.exit(1)
    main(sys.argv[1])