from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re

from enoch.paths import artifact_path
from enoch.state import atomic_write, load_json_object
from enoch.tasks.queue import TaskJob, task_queue_status


MARKDOWN_LINK_RE = re.compile(
    r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)\s]+)\)"
)
URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")
TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
SHORTLIST_ID_RE = re.compile(r"^t[1-9]\d*$")


@dataclass(frozen=True)
class ShopProduct:
    index: int
    name: str
    url: str
    store: str = ""
    price: str = ""
    variant: str = ""
    detail: str = ""


@dataclass(frozen=True)
class ShopShortlist:
    id: str
    task_id: int
    title: str
    products: tuple[ShopProduct, ...]
    notes: str = ""
    conversation_id: int | None = None

    def to_json(self) -> dict[str, object]:
        payload = asdict(self)
        payload["products"] = [
            {**asdict(product), "brand": product_brand(product)}
            for product in self.products
        ]
        return payload


def product_brand(product: ShopProduct) -> str:
    store = product.store.strip()
    if store:
        cleaned = re.sub(r"\s+online store$", "", store, flags=re.I)
        cleaned = re.sub(r"\s+(?:store|shop)$", "", cleaned, flags=re.I).strip()
        return cleaned or store
    name = product.name.strip()
    return name.split()[0] if name else f"Option {product.index}"


def extract_products(text: str) -> tuple[ShopProduct, ...]:
    table_products = _products_from_markdown_table(text)
    if table_products:
        return table_products
    return _products_from_product_urls(text)


def record_shortlist_from_task(
    job: TaskJob,
    result: str,
    *,
    root: Path | None = None,
    conversation_id: int | None = None,
) -> ShopShortlist | None:
    products = extract_products(result or job.result)
    if not products:
        return None
    shortlist = ShopShortlist(
        id=f"t{job.id}",
        task_id=job.id,
        title=_title_from_request(job.text),
        products=products,
        notes=_notes_after_products(result or job.result),
        conversation_id=conversation_id if conversation_id is not None else job.chat_id,
    )
    save_shortlist(shortlist, root=root)
    return shortlist


def backfill_shortlists_from_history(root: Path | None = None) -> tuple[ShopShortlist, ...]:
    recorded: list[ShopShortlist] = []
    for job in task_queue_status(root).history:
        if load_shortlist(f"t{job.id}", root=root) is not None:
            continue
        shortlist = record_shortlist_from_task(job, job.result, root=root)
        if shortlist is not None:
            recorded.append(shortlist)
    return tuple(recorded)


def save_shortlist(shortlist: ShopShortlist, *, root: Path | None = None) -> Path:
    path = shortlist_path(shortlist.id, root)
    atomic_write(path, json.dumps(shortlist.to_json(), indent=2) + "\n")
    _update_index(shortlist.id, root)
    return path


def load_shortlist(shortlist_id: str, *, root: Path | None = None) -> ShopShortlist | None:
    cleaned = shortlist_id.strip()
    if not SHORTLIST_ID_RE.fullmatch(cleaned):
        return None
    path = shortlist_path(cleaned, root)
    if not path.exists():
        return None
    data = load_json_object(path)
    products = tuple(
        ShopProduct(
            index=int(item.get("index") or index),
            name=str(item.get("name") or f"Option {index}"),
            url=str(item.get("url") or ""),
            store=str(item.get("store") or ""),
            price=str(item.get("price") or ""),
            variant=str(item.get("variant") or ""),
            detail=str(item.get("detail") or ""),
        )
        for index, item in enumerate(data.get("products") or [], start=1)
        if isinstance(item, dict) and str(item.get("url") or "").startswith("http")
    )
    if not products:
        return None
    return ShopShortlist(
        id=cleaned,
        task_id=int(data.get("task_id") or 0),
        title=str(data.get("title") or cleaned),
        products=products,
        notes=str(data.get("notes") or ""),
        conversation_id=_optional_int(data.get("conversation_id")),
    )


def format_shortlist_followup(
    user_text: str,
    shortlist: ShopShortlist,
    *,
    tab: int | None = None,
) -> str:
    lines = [
        "Current shop shortlist from Enoch's last product search. "
        "Use this list whenever the human says these, them, the kettles, "
        "option 2, this one, or similar. Do not ask for names or URLs that "
        "are already listed. If they ask about reviews, look the products up; "
        "do not invent ratings.",
        f"Shortlist: {shortlist.id}",
        f"Need: {shortlist.title}",
        "Products:",
    ]
    for product in shortlist.products:
        viewing = " [currently viewing this tab]" if tab == product.index else ""
        extras = [
            part
            for part in (product.price, product.store, product.variant)
            if part
        ]
        headline = f"{product.index}.{viewing} {product.name}"
        if extras:
            headline += " — " + " — ".join(extras)
        lines.append(headline)
        lines.append(f"   {product.url}")
        if product.detail:
            lines.append(f"   {product.detail}")
    if shortlist.notes:
        lines.append("Notes:")
        lines.append(shortlist.notes[:1200])
    lines.append("Human question:")
    lines.append(user_text.strip())
    return "\n".join(lines)


def resolve_followup_shortlist(
    shortlist_id: str = "",
    *,
    root: Path | None = None,
) -> ShopShortlist | None:
    cleaned = shortlist_id.strip()
    if cleaned:
        found = load_shortlist(cleaned, root=root)
        if found is not None:
            return found
    return latest_shortlist(root=root)


def latest_shortlist(*, root: Path | None = None) -> ShopShortlist | None:
    data = load_json_object(_index_path(root)) if _index_path(root).exists() else {}
    latest = str(data.get("latest") or "").strip()
    if latest:
        found = load_shortlist(latest, root=root)
        if found is not None:
            return found
    ids = [str(item) for item in data.get("ids") or [] if str(item).strip()]
    for stored_id in reversed(ids):
        found = load_shortlist(stored_id, root=root)
        if found is not None:
            return found
    return None


def shortlist_path(shortlist_id: str, root: Path | None = None) -> Path:
    return artifact_path(Path("shop") / "shortlists" / f"{shortlist_id}.json", root)


def _index_path(root: Path | None) -> Path:
    return artifact_path(Path("shop") / "shortlists" / "index.json", root)


def _update_index(shortlist_id: str, root: Path | None) -> None:
    path = _index_path(root)
    data = load_json_object(path) if path.exists() else {}
    ids = [str(item) for item in data.get("ids") or [] if str(item).strip()]
    if shortlist_id not in ids:
        ids.append(shortlist_id)
    atomic_write(
        path,
        json.dumps({"latest": shortlist_id, "ids": ids}, indent=2) + "\n",
    )


def _products_from_markdown_table(text: str) -> tuple[ShopProduct, ...]:
    lines = text.splitlines()
    tables = _markdown_tables(lines)
    if len(tables) != 1:
        return ()
    start, end = tables[0]
    rows = [_split_table_cells(line) for line in lines[start:end]]
    if len(rows) < 3 or not _is_separator_row(rows[1]):
        return ()
    headers = [cell.strip().lower() for cell in rows[0]]
    products: list[ShopProduct] = []
    for cells in rows[2:]:
        product = _product_from_cells(len(products) + 1, headers, cells)
        if product is not None:
            products.append(product)
    return tuple(products)


def _products_from_product_urls(text: str) -> tuple[ShopProduct, ...]:
    products: list[ShopProduct] = []
    seen: set[str] = set()
    for line in text.splitlines():
        url, name, leftover = _primary_link(line)
        if not url or "/products/" not in url.lower() or url in seen:
            continue
        seen.add(url)
        products.append(
            ShopProduct(
                index=len(products) + 1,
                name=name or leftover or f"Option {len(products) + 1}",
                url=url,
                store=leftover if name else "",
            )
        )
    return tuple(products)


def _product_from_cells(
    index: int,
    headers: list[str],
    cells: list[str],
) -> ShopProduct | None:
    url = ""
    name = ""
    leftover = ""
    for cell in cells:
        found_url, found_name, found_leftover = _primary_link(cell)
        if found_url:
            url = found_url
            name = found_name
            leftover = found_leftover
            break
    if not url:
        return None
    fields = {
        header: MARKDOWN_LINK_RE.sub(lambda match: match.group("label"), cell).strip()
        for header, cell in zip(headers, cells)
        if header
    }
    return ShopProduct(
        index=index,
        name=name or f"Option {index}",
        url=url,
        store=leftover or fields.get("store", ""),
        price=_first_field(fields, "price"),
        variant=_first_field(fields, "exact variant", "variant"),
        detail=_product_detail(fields),
    )


def _product_detail(fields: dict[str, str]) -> str:
    skip = {"product / merchant", "product", "merchant", "price", "exact variant", "variant"}
    parts = [
        f"{header}: {value}"
        for header, value in fields.items()
        if header not in skip and value
    ]
    return " · ".join(parts)


def _first_field(fields: dict[str, str], *names: str) -> str:
    for name in names:
        if fields.get(name):
            return fields[name]
    return ""


def _markdown_tables(lines: list[str]) -> list[tuple[int, int]]:
    tables: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        if _is_table_line(lines[index]):
            start = index
            index += 1
            while index < len(lines) and _is_table_line(lines[index]):
                index += 1
            if index - start >= 3:
                tables.append((start, index))
            continue
        index += 1
    return tables


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _split_table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(
        TABLE_SEPARATOR_CELL_RE.fullmatch(cell or "") for cell in cells
    )


def _primary_link(cell: str) -> tuple[str, str, str]:
    match = MARKDOWN_LINK_RE.search(cell)
    if match is not None:
        leftover = f"{cell[: match.start()]}{cell[match.end() :]}".strip(" \t|-–—")
        return match.group("url"), match.group("label").strip(), leftover
    url_match = URL_RE.search(cell)
    if url_match is not None:
        leftover = f"{cell[: url_match.start()]}{cell[url_match.end() :]}".strip()
        return url_match.group(0), leftover, ""
    return "", "", ""


def _title_from_request(text: str) -> str:
    cleaned = " ".join(text.split())
    for prefix in ("Use the shop skill.", "Do not create a cart or check out."):
        cleaned = cleaned.replace(prefix, "")
    return cleaned.strip()[:160] or "Shop shortlist"


def _notes_after_products(text: str) -> str:
    lines = text.splitlines()
    tables = _markdown_tables(lines)
    if len(tables) == 1:
        footer = "\n".join(lines[tables[0][1] :]).strip()
        return footer[:1500]
    return ""


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
