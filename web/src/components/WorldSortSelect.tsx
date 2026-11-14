import { useState } from "react";
import { WORLD_SORTS, type WorldSort } from "../worldList";

const STORAGE_KEY = "worldSort";
const DEFAULT_SORT: WorldSort = "status";

function stored(): WorldSort {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (WORLD_SORTS.some((s) => s.value === v)) return v as WorldSort;
  } catch {
    /* storage unavailable (private mode, blocked): use the default */
  }
  return DEFAULT_SORT;
}

/** The chosen sort, remembered per browser and shared by every world list. */
// eslint-disable-next-line react-refresh/only-export-components
export function useWorldSort(): [WorldSort, (s: WorldSort) => void] {
  const [sort, setSort] = useState<WorldSort>(stored);
  const update = (s: WorldSort) => {
    setSort(s);
    try {
      localStorage.setItem(STORAGE_KEY, s);
    } catch {
      /* not remembered, but still applied */
    }
  };
  return [sort, update];
}

export default function WorldSortSelect({
  value,
  onChange,
}: {
  value: WorldSort;
  onChange: (s: WorldSort) => void;
}) {
  return (
    <label className="world-sort">
      <span className="muted small">Sort</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as WorldSort)}
      >
        {WORLD_SORTS.map((s) => (
          <option key={s.value} value={s.value}>
            {s.label}
          </option>
        ))}
      </select>
    </label>
  );
}
