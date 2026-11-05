import { useEffect, useRef } from "react";

/** Последний экран фильтрации, пока вкладка браузера открыта.
 * Закрытие окна его не стирает. Кнопка «Сформировать» эту память не читает:
 * она снова спрашивает сервер, как и раньше. */
const views = new Map();

export function readView(key) {
  return views.get(key) ?? null;
}

export function writeView(key, value) {
  views.set(key, value);
}

export function useViewSnapshot(key, snapshot, applySnapshot) {
  const seenKey = useRef(key);
  const applyRef = useRef(applySnapshot);
  const snapRef = useRef(snapshot);
  applyRef.current = applySnapshot;
  snapRef.current = snapshot;

  useEffect(() => {
    if (seenKey.current !== key) {
      seenKey.current = key;
      applyRef.current(readView(key));
      return;
    }
    writeView(key, snapRef.current);
  }, [key, snapshot]);

  useEffect(() => {
    return () => {
      writeView(seenKey.current, snapRef.current);
    };
  }, []);
}
