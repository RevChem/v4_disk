import { useEffect, useMemo, useState } from "react";
import Link from "@mui/material/Link";

import { fetchTaskLinks } from "../api";

/** Номер задания в тексте ошибки: «задание=395525», но не «задание=4.0» (это гектары). */
const TASK_RE = /задание=(\d+)(?![\d.])/g;

export function taskIdsIn(texts) {
  const ids = [];
  const seen = new Set();
  for (const text of texts || []) {
    for (const match of String(text || "").matchAll(TASK_RE)) {
      if (!seen.has(match[1])) {
        seen.add(match[1]);
        ids.push(match[1]);
      }
    }
  }
  return ids;
}

export function useTaskLinks(texts) {
  const ids = useMemo(() => taskIdsIn(texts), [texts]);
  const key = ids.join(",");
  const [urls, setUrls] = useState({});
  useEffect(() => {
    if (!key) {
      setUrls({});
      return undefined;
    }
    let cancel = false;
    fetchTaskLinks(key.split(","))
      .then((found) => {
        if (!cancel) setUrls(found || {});
      })
      .catch(() => {});
    return () => {
      cancel = true;
    };
  }, [key]);
  return urls;
}

/** Текст ошибки, в котором «задание=…» открывает страницу задания в Cropwise. */
export function TaskText({ text, urls }) {
  const source = String(text || "");
  const parts = [];
  let last = 0;
  for (const match of source.matchAll(TASK_RE)) {
    const id = match[1];
    const href = urls?.[id];
    if (match.index > last) parts.push(source.slice(last, match.index));
    if (href) {
      parts.push(
        <Link
          key={`${match.index}-${id}`}
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()}
        >
          задание={id}
        </Link>
      );
    } else {
      parts.push(`задание=${id}`);
    }
    last = match.index + match[0].length;
  }
  if (last < source.length) parts.push(source.slice(last));
  return parts;
}
